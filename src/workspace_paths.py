"""Detect overlapping course write scopes before starting parallel workers."""

from pathlib import Path


def check_parallel_paths(video, elearning):
    ids = {part.strip() for part in video['course_ids'].split(',') if part.strip()}
    roots = [(Path(video['out_dir']).expanduser().resolve(), {'录屏'})]
    if video['mode'] != 'download':
        roots.append((Path(video['summary_dir']).expanduser().resolve(), {'笔记', '原始txt', '.icourse'}))
    for course in elearning['courses']:
        target = (Path(elearning['root']).expanduser() / course['directory']).resolve()
        for root, reserved in roots:
            if root == target or root.is_relative_to(target):
                raise ValueError('两组任务的保存范围重叠，请为 eLearning 选择独立的课件子目录。')
            if not target.is_relative_to(root):
                continue
            parts = target.relative_to(root).parts
            if not any(parts[0] == cid or parts[0].startswith(cid + '-') for cid in ids):
                continue
            if len(parts) == 1 or parts[1] in reserved:
                raise ValueError('两组任务的保存范围重叠，请为 eLearning 选择独立的课件子目录。')
