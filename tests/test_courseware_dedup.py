"""Course-scoped deduplication; all documents and filesystem mutations are fixtures."""

import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import pytest

from src.elearning_helper.config import load_config
from src.elearning_helper.demo import DemoClient, demo_config
from src.elearning_helper.state import Store
from src.elearning_helper import sync


@pytest.fixture
def setup(tmp_path):
    config = demo_config(tmp_path)
    client = DemoClient()
    store = Store(config.state_dir / "index.sqlite3")
    try:
        yield config, config.courses[0], client, store
    finally:
        store.close()


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def test_renamed_file_in_nested_courseware_directory_is_reused(setup):
    config, course, client, store = setup
    file = client.file_rows[0]
    existing = course.directory / "往年课件" / "已改名的第一讲.PDF"
    write(existing, client.bodies[file["url"]])
    first = sync.sync_file(client, store, config, course, file, dry_run=False)
    assert first["status"] == "duplicate" and first["path"] == str(existing)
    assert first["downloaded_bytes"] == file["size"]  # first index needs remote content identity
    assert list(course.directory.glob("*.pdf")) == []
    again = sync.sync_file(client, store, config, course, file, dry_run=False)
    assert again["downloaded_bytes"] == 0 and client.download_count == 1


def test_legacy_extra_root_is_never_scanned_or_reused(setup, monkeypatch):
    config, course, client, store = setup
    extra = course.directory.parent / "CLOUD"
    course = replace(course, dedup_directories=(extra,))
    file = client.file_rows[0]
    existing = extra / "renamed.pdf"
    body = client.bodies[file["url"]]
    write(existing, body)
    scandir = sync.os.scandir
    def scoped_scandir(path):
        assert not Path(path).is_relative_to(extra)
        return scandir(path)
    monkeypatch.setattr(sync.os, "scandir", scoped_scandir)
    first = sync.sync_file(client, store, config, course, file, dry_run=False)
    assert first["status"] == "downloaded" and Path(first["path"]).parent == course.directory
    again = sync.sync_file(client, store, config, course, file, dry_run=False)
    assert again["downloaded_bytes"] == 0 and client.download_count == 1
    assert existing.read_bytes() == body

def test_indexed_file_renamed_and_moved_is_recovered_without_network(setup):
    config, course, client, store = setup
    file = client.file_rows[0]
    first = sync.sync_file(client, store, config, course, file, dry_run=False)
    moved = course.directory / "章节" / "重命名.pdf"
    moved.parent.mkdir()
    Path(first["path"]).rename(moved)
    client.open = Mock(side_effect=AssertionError("unchanged source must not download again"))
    preview = sync.sync_file(client, store, config, course, file, dry_run=True)
    assert preview["path"] == str(moved)
    assert store.file(course.id, "1")["path"] == first["path"]  # read-only preview
    result = sync.sync_file(client, store, config, course, file, dry_run=False)
    assert result["downloaded_bytes"] == 0 and store.file(course.id, "1")["path"] == str(moved)


def test_existing_duplicates_are_not_deleted_or_multiplied(setup):
    config, course, client, store = setup
    file = client.file_rows[0]
    originals = [course.directory / "A.pdf", course.directory / "B.pdf"]
    for path in originals:
        write(path, client.bodies[file["url"]])
    result = sync.sync_file(client, store, config, course, file, dry_run=False)
    assert result["status"] == "duplicate"
    assert sorted(course.directory.iterdir()) == originals
    assert all(path.read_bytes() == client.bodies[file["url"]] for path in originals)


def test_renamed_copy_remote_version_change_updates_index_after_one_comparison(setup):
    config, course, client, store = setup
    file = client.file_rows[0]
    first = sync.sync_file(client, store, config, course, file, dry_run=False)
    renamed = course.directory / "renamed.pdf"
    Path(first["path"]).rename(renamed)
    changed = {**file, "modified_at": "v2"}
    compared = sync.sync_file(client, store, config, course, changed, dry_run=False)
    assert compared["status"] == "duplicate" and compared["path"] == str(renamed)
    assert compared["downloaded_bytes"] == file["size"]
    third = sync.sync_file(client, store, config, course, changed, dry_run=False)
    assert third["downloaded_bytes"] == 0 and client.download_count == 2
    assert len(list(course.directory.iterdir())) == 1


def test_same_name_annotated_copy_takes_priority_over_other_hash_matches(setup):
    config, course, client, store = setup
    file = client.file_rows[0]
    first = sync.sync_file(client, store, config, course, file, dry_run=False)
    intact = course.directory / "副本" / "原始.pdf"
    write(intact, client.bodies[file["url"]])
    edited = Path(first["path"])
    edited.write_bytes(b"local changes are preserved")
    client.open = Mock(side_effect=AssertionError("must reuse intact approved copy"))
    result = sync.sync_file(client, store, config, course, file, dry_run=False)
    assert result["path"] == str(edited) and result["status"] == "name_preserved" and result["downloaded_bytes"] == 0
    assert edited.read_bytes() == b"local changes are preserved"


def test_local_changes_are_preserved_without_claiming_integrity_or_redownloading(setup):
    config, course, client, store = setup
    file = client.file_rows[0]
    first = sync.sync_file(client, store, config, course, file, dry_run=False)
    original = Path(first["path"])
    original.write_bytes(b"X" * file["size"])
    second = sync.sync_file(client, store, config, course, file, dry_run=False)
    assert second["status"] == "name_preserved" and second["path"] == first["path"]
    assert original.read_bytes() == b"X" * file["size"]
    assert second["downloaded_bytes"] == 0 and client.download_count == 1
    assert store.file(course.id, "1")["sha256"] == sync.hashlib.sha256(client.bodies[file["url"]]).hexdigest()


def test_missing_remote_version_cannot_claim_source_unchanged(setup):
    config, course, client, store = setup
    file = {**client.file_rows[0], "modified_at": None, "updated_at": None}
    first = sync.sync_file(client, store, config, course, file, dry_run=False)
    second = sync.sync_file(client, store, config, course, file, dry_run=False)
    assert first["version_unconfirmed"] and not second["local_content_verified"]
    assert second["status"] == "name_preserved" and client.download_count == 1
    assert len(list(course.directory.iterdir())) == 1


@pytest.mark.parametrize("directory", [".git", ".venv", "venv", "node_modules", "__pycache__"])
def test_unrelated_directories_are_pruned_before_scanning_or_hashing(setup, monkeypatch, directory):
    config, course, client, store = setup
    file = client.file_rows[0]
    unrelated = course.directory / directory
    write(unrelated / "第一讲.pdf", client.bodies[file["url"]])
    scandir = sync.os.scandir
    def scoped_scandir(path):
        assert not Path(path).is_relative_to(unrelated), "must not descend into unrelated tree"
        return scandir(path)
    monkeypatch.setattr(sync.os, "scandir", scoped_scandir)
    result = sync.sync_file(client, store, config, course, file, dry_run=False)
    assert result["status"] == "downloaded"
    assert (unrelated / "第一讲.pdf").read_bytes() == client.bodies[file["url"]]


def test_index_path_outside_course_and_symlink_subtree_are_never_read(setup, monkeypatch):
    config, course, client, store = setup
    file = client.file_rows[0]
    external = config.root / "其他课程" / "第一讲.pdf"
    write(external, client.bodies[file["url"]])
    digest = sync.sha256(external)
    store.save_file(course.id, str(file["id"]), sync.version(file), file["size"], digest, external)
    course.directory.mkdir(parents=True)
    (course.directory / "link").symlink_to(external.parent, target_is_directory=True)
    sha = sync.sha256
    def scoped_hash(path):
        assert Path(path).is_relative_to(course.directory) and "link" not in Path(path).parts
        return sha(path)
    monkeypatch.setattr(sync, "sha256", scoped_hash)
    result = sync.sync_file(client, store, config, course, file, dry_run=False)
    assert result["status"] == "downloaded" and Path(result["path"]).parent == course.directory


def test_dry_run_reports_renamed_candidates_without_hash_or_download(setup, monkeypatch):
    config, course, client, store = setup
    file = client.file_rows[0]
    write(course.directory / "章节" / "改名.pdf", client.bodies[file["url"]])
    monkeypatch.setattr(sync, "sha256", lambda *_: pytest.fail("no known remote hash; metadata only in preview"))
    result = sync.sync_file(client, store, config, course, file, dry_run=True)
    assert result["status"] == "would_check_content" and "候选 1 个" in result["note"]
    assert client.download_count == 0 and store.file(course.id, "1") is None


def test_same_title_but_different_size_is_not_assumed_duplicate(setup):
    config, course, client, store = setup
    file = client.file_rows[0]
    existing = course.directory / "第一讲 (1).pdf"
    write(existing, b"%PDF-1.4\na different older revision\n%%EOF\n")
    before = existing.read_bytes()
    preview = sync.sync_file(client, store, config, course, file, dry_run=True)
    assert preview["status"] == "would_download"
    result = sync.sync_file(client, store, config, course, file, dry_run=False)
    assert result["status"] == "downloaded" and existing.read_bytes() == before


def config_file(tmp_path, extra):
    data = {"base_url": "https://elearning.fudan.edu.cn", "root": str(tmp_path / "courses"),
            "courses": [{"id": "114667", "name": "fixture", "directory": "37345-NLP/课件", "dedup_directories": extra}]}
    path = tmp_path / "config.json"
    path.write_text(json.dumps(data))
    return path


@pytest.mark.parametrize("scope", ["other-course/课件", "37345-NLP", "37345-NLP/code", "37345-NLP/作业", "37345-NLP/../outside", "/tmp/课件"])
def test_extra_scopes_cannot_escape_or_scan_whole_course(tmp_path, scope):
    with pytest.raises(ValueError):
        load_config(config_file(tmp_path, [scope]))


def test_only_target_scope_and_explicit_id_mapping(tmp_path):
    for legacy in ["CLOUD", "课程ppt", "资料", "旧课件", "课件/nested"]:
        with pytest.raises(ValueError, match="不能配置额外"):
            load_config(config_file(tmp_path, ["37345-NLP/" + legacy]))
    actual = load_config(config_file(tmp_path, []))
    assert {c.id: str(c.directory.relative_to(actual.root)) for c in actual.courses} == {
        "114667": "37345-NLP/课件"}
    assert all(c.dedup_directories == () for c in actual.courses)

def test_external_index_path_does_not_skip_new_destination_download(setup, monkeypatch):
    config, course, client, store = setup
    file = client.file_rows[0]
    first = sync.sync_file(client, store, config, course, file, dry_run=False)
    original = Path(first["path"])
    before = original.read_bytes()
    new_course = replace(course, directory=course.directory.parent / "新的课件", dedup_directories=(course.directory,))
    sha = sync.sha256
    def scoped_hash(path):
        assert Path(path).is_relative_to(new_course.directory), "outside index must not be read"
        return sha(path)
    monkeypatch.setattr(sync, "sha256", scoped_hash)
    preview = sync.sync_file(client, store, config, new_course, file, dry_run=True)
    assert preview["status"] == "would_download" and client.download_count == 1
    assert store.file(course.id, str(file["id"]))["path"] == first["path"]
    result = sync.sync_file(client, store, config, new_course, file, dry_run=False)
    assert result["status"] == "downloaded" and Path(result["path"]).parent == new_course.directory
    assert result["downloaded_bytes"] == file["size"] and original.read_bytes() == before
    assert client.download_count == 2

def test_annotations_inside_current_scope_are_preserved_even_after_remote_version_changes(setup):
    config, course, client, store = setup
    file = client.file_rows[0]
    first = sync.sync_file(client, store, config, course, file, dry_run=False)
    original = Path(first["path"])
    annotated = original.read_bytes() + b"local notes"
    original.write_bytes(annotated)
    unchanged = sync.sync_file(client, store, config, course, file, dry_run=False)
    assert unchanged["status"] == "name_preserved" and client.download_count == 1
    remote = b"%PDF-1.4\nnew teaching content\n%%EOF\n"
    client.bodies[file["url"]] = remote
    result = sync.sync_file(client, store, config, course,
                            {**file, "modified_at": "v2", "size": len(remote)}, dry_run=False)
    assert result["status"] == "name_preserved" and result["path"] == first["path"]
    assert original.read_bytes() == annotated and client.download_count == 1

def test_unindexed_same_name_is_always_preserved_even_with_legacy_compare_flag(setup):
    config, course, client, store = setup
    file = client.file_rows[0]
    original = course.directory / "章节" / file["display_name"]
    body = b"%PDF-1.4\npossible user notes\n%%EOF\n"
    write(original, body)
    for dry_run in (True, False):
        result = sync.sync_file(client, store, config, course, file, dry_run=dry_run)
        assert result["status"] == "name_preserved" and client.download_count == 0
        assert store.file(course.id, "1") is None
    compared = sync.sync_file(client, store, config, course, file, dry_run=False, compare_existing=True)
    assert compared["status"] == "name_preserved" and original.read_bytes() == body
    assert compared["path"] == str(original) and client.download_count == 0
    assert store.file(course.id, "1") is None


def test_unindexed_same_name_identical_content_is_not_falsely_indexed(setup):
    config, course, client, store = setup
    file = client.file_rows[0]
    original = course.directory / file["display_name"]
    write(original, client.bodies[file["url"]])
    assert sync.sync_file(client, store, config, course, file, dry_run=False)["status"] == "name_preserved"
    result = sync.sync_file(client, store, config, course, file, dry_run=False, compare_existing=True)
    assert result["status"] == "name_preserved" and list(course.directory.iterdir()) == [original]
    assert store.file(course.id, "1") is None and client.download_count == 0


def test_remote_metadata_only_change_does_not_multiply_annotated_copies(setup):
    config, course, client, store = setup
    file = client.file_rows[0]
    first = sync.sync_file(client, store, config, course, file, dry_run=False)
    original = Path(first["path"])
    annotated = original.read_bytes() + b"annotations preserved"
    original.write_bytes(annotated)
    changed = {**file, "modified_at": "v2-metadata-only"}
    result = sync.sync_file(client, store, config, course, changed, dry_run=False)
    assert result["status"] == "name_preserved" and result["downloaded_bytes"] == 0
    assert list(course.directory.iterdir()) == [original] and original.read_bytes() == annotated
    again = sync.sync_file(client, store, config, course, changed, dry_run=False)
    assert again["downloaded_bytes"] == 0 and client.download_count == 1


def test_course_scopes_cannot_overlap(tmp_path):
    path = config_file(tmp_path, [])
    data = json.loads(path.read_text())
    data["courses"].append({"id": "9", "name": "different course", "directory": "37345-NLP/课件/nested"})
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="重叠"):
        load_config(path)


@pytest.mark.parametrize("legacy", ["CLOUD", "课程ppt", "资料"])
def test_external_same_name_is_not_a_duplicate_or_review_blocker(setup, monkeypatch, legacy):
    config, course, client, store = setup
    file = client.file_rows[0]
    external = course.directory.parent / legacy
    original = external / file["display_name"]
    body = client.bodies[file["url"]]
    write(original, body)
    course = replace(course, dedup_directories=(external,))
    scandir = sync.os.scandir
    def scoped_scandir(path):
        assert not Path(path).is_relative_to(external)
        return scandir(path)
    monkeypatch.setattr(sync.os, "scandir", scoped_scandir)
    preview = sync.sync_file(client, store, config, course, file, dry_run=True)
    assert preview["status"] == "would_download" and client.download_count == 0
    result = sync.sync_file(client, store, config, course, file, dry_run=False)
    assert result["status"] == "downloaded" and original.read_bytes() == body


def test_external_index_history_can_only_reuse_matching_copy_inside_current_scope(setup):
    config, course, client, store = setup
    file = client.file_rows[0]
    external = course.directory.parent / "CLOUD" / file["display_name"]
    body = client.bodies[file["url"]]
    write(external, body)
    store.save_file(course.id, str(file["id"]), sync.version(file), file["size"],
                    sync.hashlib.sha256(body).hexdigest(), external)
    internal = course.directory / "章节" / "renamed.pdf"
    write(internal, body)
    client.open = Mock(side_effect=AssertionError("must reuse internal matching content"))
    result = sync.sync_file(client, store, config, course, file, dry_run=False)
    assert result["status"] == "duplicate" and result["path"] == str(internal)
    assert result["downloaded_bytes"] == 0


def test_external_index_does_not_override_same_name_internal_annotations(setup):
    config, course, client, store = setup
    file = client.file_rows[0]
    external = course.directory.parent / "CLOUD" / file["display_name"]
    body = client.bodies[file["url"]]
    write(external, body)
    store.save_file(course.id, str(file["id"]), sync.version(file), file["size"],
                    sync.hashlib.sha256(body).hexdigest(), external)
    internal = course.directory / file["display_name"]
    write(internal, body + b"possible user notes")
    result = sync.sync_file(client, store, config, course, file, dry_run=False)
    assert result["status"] == "name_preserved" and client.download_count == 0
    assert internal.read_bytes().endswith(b"possible user notes")


def test_symlink_file_into_old_directory_cannot_reuse_external_index(setup):
    config, course, client, store = setup
    file = client.file_rows[0]
    external = course.directory.parent / "资料" / file["display_name"]
    body = client.bodies[file["url"]]
    write(external, body)
    course.directory.mkdir(parents=True)
    link = course.directory / file["display_name"]
    link.symlink_to(external)
    store.save_file(course.id, str(file["id"]), sync.version(file), file["size"],
                    sync.hashlib.sha256(body).hexdigest(), link)
    with pytest.raises(sync.FileRejected, match="不是普通文件"):
        sync.sync_file(client, store, config, course, file, dry_run=False)
    assert client.download_count == 0
    assert link.is_symlink() and external.read_bytes() == body


def test_destination_cannot_accidentally_be_a_runtime_directory(tmp_path):
    path = config_file(tmp_path, [])
    data = json.loads(path.read_text())
    data["courses"][0]["directory"] = "37345-NLP/.venv"
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="保存位置"):
        load_config(path)
