# SPDX-FileCopyrightText: 2026 The Switchyard contributors
#
# SPDX-License-Identifier: Apache-2.0

"""Upgrade bundles: build, verify, and refusal of tampered or hostile archives (ADR 0205)."""

import io
import tarfile
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from switchyard.domain.bundle import BundleError
from switchyard.domain.releases import ReleaseNotes, public_key_b64
from switchyard.services.release_bundle import Input, build, unpack, verify

KEY = Ed25519PrivateKey.generate()
TRUSTED = {"k1": public_key_b64(KEY)}


def make(tmp_path: Path) -> Path:
    compose = tmp_path / "compose.yaml"
    compose.write_text("services: {}\n")
    image = tmp_path / "controller.tar"
    image.write_bytes(b"image-bytes" * 1000)
    out = tmp_path / "switchyard-0.2.0.sybundle"
    build(
        out,
        version="0.2.0",
        schema_head="0046",
        created_at="2026-10-13T04:00:00Z",
        inputs=[
            Input("compose", compose, "compose.yaml"),
            Input("controller-image", image, "controller.tar"),
        ],
        key=KEY,
        key_id="k1",
        upgrade_from=["0.1.0"],
        notes=ReleaseNotes(action_required=["Back up first"]),
    )
    return out


def rewrite(src: Path, dst: Path, edit: dict[str, bytes | None], extra: dict[str, bytes]) -> None:
    """Copy ``src`` to ``dst`` replacing, dropping (None) or adding members."""
    with tarfile.open(src) as tin, tarfile.open(dst, "w") as tout:
        for member in tin.getmembers():
            data = tin.extractfile(member).read()  # type: ignore[union-attr]
            if member.name in edit:
                if edit[member.name] is None:
                    continue
                data = edit[member.name]  # type: ignore[assignment]
            info = tarfile.TarInfo(member.name)
            info.size = len(data)
            tout.addfile(info, io.BytesIO(data))
        for name, data in extra.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tout.addfile(info, io.BytesIO(data))


def test_a_built_bundle_verifies_and_unpacks(tmp_path: Path) -> None:
    out = make(tmp_path)
    manifest = verify(out, TRUSTED)
    assert manifest.version == "0.2.0" and len(manifest.files) == 2
    assert manifest.notes.action_required == ["Back up first"]
    dest = tmp_path / "unpacked"
    unpack(out, dest, TRUSTED)
    assert (dest / "files/compose/compose.yaml").read_text() == "services: {}\n"
    assert (dest / "files/controller-image/controller.tar").stat().st_size == 11000
    with pytest.raises(BundleError, match="already exists"):
        unpack(out, dest, TRUSTED)


def test_an_untrusted_or_wrong_key_is_refused(tmp_path: Path) -> None:
    out = make(tmp_path)
    with pytest.raises(BundleError, match="not trusted"):
        verify(out, {})
    with pytest.raises(BundleError, match="does not verify"):
        verify(out, {"k1": public_key_b64(Ed25519PrivateKey.generate())})


def test_a_changed_file_or_manifest_is_refused(tmp_path: Path) -> None:
    out = make(tmp_path)
    bad_file = tmp_path / "bad1.sybundle"
    rewrite(out, bad_file, {"files/compose/compose.yaml": b"services: {evil: {}}\n"}, {})
    with pytest.raises(BundleError, match="does not match the manifest"):
        verify(bad_file, TRUSTED)
    bad_manifest = tmp_path / "bad2.sybundle"
    rewrite(out, bad_manifest, {"manifest.json": b'{"format":1}'}, {})
    with pytest.raises(BundleError, match="does not verify"):
        verify(bad_manifest, TRUSTED)


def test_extra_missing_and_hostile_members_are_refused(tmp_path: Path) -> None:
    out = make(tmp_path)
    extra = tmp_path / "extra.sybundle"
    rewrite(out, extra, {}, {"files/compose/extra.sh": b"rm -rf /"})
    with pytest.raises(BundleError, match="unexpected member"):
        verify(extra, TRUSTED)
    missing = tmp_path / "missing.sybundle"
    rewrite(out, missing, {"files/compose/compose.yaml": None}, {})
    with pytest.raises(BundleError, match="missing"):
        verify(missing, TRUSTED)
    escape = tmp_path / "escape.sybundle"
    rewrite(out, escape, {}, {"../../etc/cron.d/x": b"x"})
    with pytest.raises(BundleError, match="unexpected member"):
        verify(escape, TRUSTED)
    link = tmp_path / "link.sybundle"
    with tarfile.open(out) as tin, tarfile.open(link, "w") as tout:
        for member in tin.getmembers():
            tout.addfile(member, tin.extractfile(member))
        sym = tarfile.TarInfo("files/compose/compose.yaml")
        sym.type, sym.linkname = tarfile.SYMTYPE, "/etc/passwd"
        tout.addfile(sym)
    with pytest.raises(BundleError):
        verify(link, TRUSTED)


def test_not_a_bundle_is_refused(tmp_path: Path) -> None:
    junk = tmp_path / "junk"
    junk.write_bytes(b"this is not a tar archive" * 50)
    with pytest.raises(BundleError, match="not a readable bundle"):
        verify(junk, TRUSTED)


def test_unsafe_names_are_refused_at_build_time(tmp_path: Path) -> None:
    src = tmp_path / "f"
    src.write_text("x")
    with pytest.raises(BundleError, match="unsafe"):
        build(
            tmp_path / "o",
            version="0.2.0",
            schema_head="0046",
            created_at="x",
            inputs=[Input("compose", src, "../escape")],
            key=KEY,
            key_id="k1",
        )
