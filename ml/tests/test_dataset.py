import numpy as np
import pytest

from pothole_ml.config import PipelineConfig
from pothole_ml.dataset import (Chunk, Event, Label, Segment, align_labels, build_segments,
                                make_windows, parse_started_at, window_features)
from pothole_ml.synth import random_rotation

G = 9.81


def _chunk(start_ms: int, n: int = 80, z: float = G) -> Chunk:
    acc = np.zeros((n, 3), np.float32)
    acc[:, 2] = z
    return Chunk(start_ms, acc, np.zeros((n, 2)))


def _segment(n: int, start_ms: int = 0, acc: np.ndarray | None = None) -> Segment:
    if acc is None:
        acc = np.zeros((n, 3), np.float32)
        acc[:, 2] = G
    latlon = np.tile([54.0, 25.0], (n, 1))
    return Segment(0, start_ms + 25 * np.arange(n, dtype=np.int64), acc, latlon)


def test_parse_started_at_formats():
    assert parse_started_at("2026-10-10T11:11:50.974Z") == 1791630710974
    assert parse_started_at("2026-10-10T11:11:50.974+00:00") == 1791630710974
    assert parse_started_at(1791630710974) == 1791630710974
    assert parse_started_at("1791630710974") == 1791630710974


def test_build_segments_contiguous_and_gaps():
    chunks = [_chunk(0), _chunk(2000), _chunk(4000)]
    segs = build_segments(chunks)
    assert len(segs) == 1 and len(segs[0]) == 240
    assert np.all(np.diff(segs[0].t_ms) == 25)

    # a missing chunk splits the segment
    segs = build_segments([_chunk(0), _chunk(4000)])
    assert [len(s) for s in segs] == [80, 80]
    assert segs[1].start_ms == 4000 and segs[1].segment_id == 1

    # unsorted input is sorted, and 30 ms of timer jitter stays contiguous
    segs = build_segments([_chunk(2030), _chunk(0)])
    assert len(segs) == 1 and segs[0].t_ms[80] == 2030


def test_build_segments_skips_duplicate_chunks(capsys):
    segs = build_segments([_chunk(0), _chunk(0), _chunk(2000)])
    assert len(segs) == 1 and len(segs[0]) == 160
    assert "duplicate" in capsys.readouterr().err


def _spiky_segment(spike_samples: dict[int, float], n: int = 400) -> Segment:
    acc = np.zeros((n, 3), np.float32)
    acc[:, 2] = G
    rng = np.random.default_rng(0)
    acc += rng.normal(0, 0.05, acc.shape)
    for i, amp in spike_samples.items():
        acc[i, 2] += amp
    return _segment(n, acc=acc)


def test_align_labels_peak_lookback():
    seg = _spiky_segment({40: 6.0})            # spike at t=1000 ms
    cfg = PipelineConfig()
    events, stats = align_labels([Label(1600, 0, 0, 1)], [seg], cfg)
    assert stats.matched == 1 and stats.unmatched == 0
    assert events[0].t_ms == 1000 and events[0].cls == 1
    assert stats.offsets_ms == [600]


def test_align_labels_unmatched_when_no_segment():
    seg = _spiky_segment({40: 6.0})
    events, stats = align_labels([Label(50_000, 0, 0, 2)], [seg], PipelineConfig())
    assert events == [] and stats.unmatched == 1


def test_align_labels_two_close_labels_two_peaks():
    seg = _spiky_segment({40: 6.0, 56: 5.0})   # peaks at 1000 and 1400 ms
    cfg = PipelineConfig(min_event_separation_ms=300)
    labels = [Label(1700, 0, 0, 1), Label(2100, 0, 0, 2)]
    events, stats = align_labels(labels, [seg], cfg)
    assert stats.matched == 2
    assert sorted(e.t_ms for e in events) == [1000, 1400]


def test_align_labels_fixed_method():
    seg = _spiky_segment({})
    cfg = PipelineConfig(align_method="fixed", fixed_offset_ms=700)
    events, _ = align_labels([Label(2000, 0, 0, 1)], [seg], cfg)
    assert events[0].t_ms == 1300


def test_make_windows_labels():
    cfg = PipelineConfig(window_len=64, stride=8, center_tol_ms=300, ignore_margin_ms=200)
    seg = _segment(400)                        # 0 .. 9975 ms
    ev = Event(t_ms=4000, lat=0, lon=0, cls=2, segment_id=0, sample_idx=160, peak_value=1,
               label_t_ms=4500)
    ws = make_windows(seg, [ev], cfg)
    assert len(ws) == (400 - 64) // 8 + 1
    centered = np.abs(ws.t_center - 4000) <= 300
    assert np.all(ws.y[centered] == 2) and centered.sum() >= 3
    t_start = ws.t_center - (63 * 25) // 2
    t_end = t_start + 63 * 25
    inside = (4000 >= t_start - 200) & (4000 <= t_end + 200)
    assert np.all(ws.y[inside & ~centered] == -1)
    assert np.all(ws.y[~inside] == 0)
    assert np.all(ws.event_t_ms[inside] == 4000) and np.all(ws.event_t_ms[~inside] == -1)
    # every window touching the event shares one group id
    assert len(np.unique(ws.group_id[inside])) == 1


def test_make_windows_short_segment():
    ws = make_windows(_segment(30), [], PipelineConfig())
    assert len(ws) == 0


def test_window_features_rotation_invariant():
    rng = np.random.default_rng(1)
    w = rng.normal(0, 0.5, size=(5, 64, 3)).astype(np.float32)
    w[:, :, 2] += G
    w[:, 30, 2] += 5.0
    f0 = window_features(w, scale=0.3)
    assert f0.shape == (5, 3, 64)
    R = random_rotation(rng)
    f1 = window_features(w @ R.T.astype(np.float32), scale=0.3)
    assert np.allclose(f0, f1, atol=1e-3)
    # the signed vertical channel keeps the sign of the impact
    assert f0[0, 0, 30] > 0


@pytest.mark.parametrize("value", ["not a date", "2026-13-40T00:00:00Z"])
def test_parse_started_at_rejects_garbage(value):
    with pytest.raises(ValueError):
        parse_started_at(value)
