from types import SimpleNamespace

import pytest

from app.core.errors import AppError
from app.services.automatic_final import export_target, timeline_parameters


def test_word_export_uses_vertical_resolution_and_the_screenplay_clock():
    shots = [SimpleNamespace(id=f's{i}', aspect_ratio='9:16', duration_seconds=duration)
             for i, duration in enumerate([2, 3, 3, 3])]
    script = SimpleNamespace(parsed_json={'project': {'aspect_ratio': '9:16',
        'constraints': '保留实拍质感\n技术参数：4K分辨率、60fps、全片11秒。'}})
    videos = [SimpleNamespace(id=f'v{i}', shot_id=shot.id) for i, shot in enumerate(shots)]
    probes = [{'duration': 4.0, 'fps': 24} for _ in shots]
    clips, transitions, output = timeline_parameters(export_target(script, shots), videos, probes)
    assert [clip['video_id'] for clip in clips] == ['v0', 'v1', 'v2', 'v3']
    assert [clip['trim_end'] for clip in clips] == [2, 3, 3, 3]
    assert [edge['type'] for edge in transitions] == ['cut', 'cut', 'cut']
    assert (output['width'], output['height'], output['fps']) == (2160, 3840, 60)
    assert output['audio_policy'] == 'preserve' and not output['subtitles']
    probes[2]['duration'] = 2.7
    with pytest.raises(AppError) as error:
        timeline_parameters(export_target(script, shots), videos, probes)
    assert error.value.code == 'automatic_final_short_clip'


def test_requested_overlap_uses_only_real_tail_to_keep_the_total_duration():
    shots = [SimpleNamespace(id=f's{i}', aspect_ratio='16:9', duration_seconds=2) for i in range(2)]
    videos = [SimpleNamespace(id=f'v{i}', shot_id=shot.id) for i, shot in enumerate(shots)]
    target = export_target(None, shots, ['交叉淡化 0.5 秒'])
    probes = [{'duration': 3, 'fps': 24}, {'duration': 2, 'fps': 24}]
    clips, transitions, _ = timeline_parameters(target, videos, probes)
    assert clips[0]['trim_end'] == 2.5 and transitions[0]['type'] == 'crossfade'
    assert sum(clip['trim_end'] for clip in clips) - transitions[0]['duration'] == 4
    probes[0]['duration'] = 2
    clips, transitions, _ = timeline_parameters(target, videos, probes)
    assert clips[0]['trim_end'] == 2 and transitions[0]['type'] == 'cut'
