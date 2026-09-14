"""Conservative, digest-bound README hints for the procedure editor.

A hint is evidence for the user to review, never a substitute for procedure
validation. Only explicit minimum OPatch requirements are extracted; rollback
instructions and installed versions are deliberately outside this interface.
"""
from __future__ import annotations

import hashlib
from html.parser import HTMLParser
import re
import unicodedata

import evidence
import remote

MAX_README_BYTES = 2 * 1024 * 1024
_SHA256 = re.compile(r"[a-f0-9]{64}\Z")
_ROOT_README = re.compile(r"readme(?:[._-][^/]+)?\Z", re.IGNORECASE)
_VERSION = r"(?P<version>[0-9]+(?:\.[0-9]+){2,4})(?!\d|\.\d)"
_OPATCH = r"(?:the\s+)?OPatch\s+(?:utility\s+)?(?:version\s+)?"
_MINIMUM = r"(?:or\s+(?:later|higher|newer)|and\s+(?:above|later))"
_APPLY = r"to\s+(?:apply|install)\s+(?:this|the)\s+patch"
_REQUIREMENTS = tuple(re.compile(pattern, re.IGNORECASE) for pattern in (
    rf"(?:You\s+)?must\s+use\s+{_OPATCH}{_VERSION}\s+{_MINIMUM}(?:\s+{_APPLY})?[.!]?",
    rf"Use\s+{_OPATCH}{_VERSION}\s+{_MINIMUM}\s+{_APPLY}[.!]?",
    rf"{_OPATCH}{_VERSION}\s+{_MINIMUM}\s+is\s+required(?:\s+{_APPLY})?[.!]?",
    rf"(?:The\s+)?minimum\s+(?:required\s+)?OPatch\s+(?:utility\s+)?version(?:\s+(?:is|must\s+be)|\s*:)\s*{_VERSION}[.!]?",
))
_NON_REQUIREMENT = re.compile(
    r"\b(?:example|examples|sample|illustration|installed|output|rollback|rolling\s+back|"
    r"uninstall|previous|previously|formerly)\b|\be\.g\.", re.IGNORECASE
)


class _ReadmeText(HTMLParser):
    """Preserve inline text, separate blocks, and omit executable/example text."""

    _BLOCKS = frozenset({
        "address", "article", "aside", "blockquote", "br", "dd", "div", "dl", "dt",
        "fieldset", "figcaption", "figure", "footer", "form", "h1", "h2", "h3", "h4",
        "h5", "h6", "header", "hr", "li", "main", "nav", "ol", "p", "section", "table",
        "tbody", "td", "th", "thead", "tr", "ul",
    })
    _OMIT = frozenset({"script", "style", "template", "pre", "samp"})

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.omitted: list[str] = []
        self.heading: str | None = None
        self.heading_parts: list[str] = []
        self.example_heading_level: int | None = None

    def handle_starttag(self, tag, attrs):
        if self.omitted:
            if tag in self._OMIT:
                self.omitted.append(tag)
            return
        if tag in self._OMIT:
            self.omitted.append(tag)
            self.parts.append("\x1e")
        elif tag in self._BLOCKS:
            self.parts.append("\x1e")
        if re.fullmatch(r"h[1-6]", tag):
            self.heading = tag
            self.heading_parts = []

    def handle_endtag(self, tag):
        if self.omitted:
            if tag == self.omitted[-1]:
                self.omitted.pop()
            return
        if tag == self.heading:
            title = " ".join("".join(self.heading_parts).split())
            level = int(tag[1])
            if re.search(r"\b(?:examples?|samples?|illustrations?)\b", title, re.IGNORECASE):
                self.example_heading_level = min(self.example_heading_level or level, level)
            elif self.example_heading_level is not None and level <= self.example_heading_level:
                self.example_heading_level = None
            self.heading = None
            self.heading_parts = []
        if tag in self._BLOCKS:
            self.parts.append("\x1e")

    def handle_data(self, data):
        if not self.omitted:
            if self.heading is not None:
                self.heading_parts.append(data)
            if self.example_heading_level is None:
                self.parts.append(data)


def _safe_path(value: object, *, absolute: bool = False) -> bool:
    if not isinstance(value, str) or not value or "\\" in value:
        return False
    if any(unicodedata.category(char).startswith("C") for char in value):
        return False
    if value.startswith("/") != absolute:
        return False
    parts = value[1:].split("/") if absolute else value.split("/")
    return all(part and part not in (".", "..") for part in parts)


def _sha256(value: object) -> bool:
    return isinstance(value, str) and _SHA256.fullmatch(value) is not None


def _decode_readme(payload: bytes) -> str:
    # Legacy Oracle READMEs commonly declare Windows-1252. Do not transform
    # bytes before the caller verifies their inspected digest.
    charset = re.search(
        br"<meta\b[^>]*\bcharset\s*=\s*[\"']?\s*([A-Za-z0-9._-]+)",
        payload[:8192], re.IGNORECASE,
    )
    name = charset.group(1).decode("ascii").lower() if charset else "utf-8"
    encodings = {
        "utf-8": "utf-8-sig", "utf8": "utf-8-sig", "us-ascii": "ascii", "ascii": "ascii",
        "windows-1252": "cp1252", "cp1252": "cp1252", "iso-8859-1": "latin-1",
        "iso8859-1": "latin-1", "latin1": "latin-1", "latin-1": "latin-1",
    }
    try:
        return payload.decode(encodings[name])
    except (KeyError, UnicodeDecodeError):
        raise remote.RemoteError(
            "invalid_readme_encoding",
            "The inspected README encoding is unsupported or invalid. Review the README manually.",
        ) from None


def _extract_requirement(payload: bytes) -> tuple[str | None, str | None, list[str]]:
    parser = _ReadmeText()
    parser.feed(_decode_readme(payload))
    parser.close()
    matches: list[tuple[str, str]] = []
    unclear_requirement = False
    for block in "".join(parser.parts).split("\x1e"):
        text = " ".join(block.split())
        for sentence in re.split(r"(?<=[.!?])\s+", text):
            if not sentence or len(sentence) > 400 or _NON_REQUIREMENT.search(sentence):
                continue
            for pattern in _REQUIREMENTS:
                match = pattern.fullmatch(sentence)
                if match:
                    matches.append((match.group("version"), sentence))
                    break
            else:
                # A conditional or otherwise unsupported requirement must not
                # disappear merely because another sentence is easy to parse.
                if (re.search(r"\bOPatch\b", sentence, re.IGNORECASE)
                        and re.search(r"\b(?:must|minimum|required)\b", sentence, re.IGNORECASE)
                        and re.search(r"\d+(?:\.\d+){2,}", sentence)):
                    unclear_requirement = True
    versions = {version for version, _ in matches}
    if unclear_requirement:
        return None, None, [
            "The selected README contains a conditional or unclear OPatch requirement. Review the applicable requirement and enter it manually."
        ]
    if not versions:
        return None, None, [
            "No unambiguous explicit minimum OPatch requirement was found. Review the selected README and enter the requirement manually."
        ]
    if len(versions) != 1:
        return None, None, [
            "The selected README states different minimum OPatch versions. Review the applicable requirement and enter it manually."
        ]
    version, sentence = matches[0]
    return version, sentence, []


def get_hints(host_id: str, host: dict, readme_identifier: str = "") -> dict:
    """Read exactly one inspected README and return reviewable procedure hints."""
    document = evidence.read_evidence(host_id, "artifact")
    artifact = document.get("artifact") if isinstance(document, dict) else None
    if (not isinstance(artifact, dict) or artifact.get("status") != "ready_for_catalog"
            or not _sha256(artifact.get("sha256")) or not _safe_path(artifact.get("path"), absolute=True)):
        raise remote.RemoteError(
            "artifact_inspection_required", "A valid inspected artifact is required. Run artifact inspection again."
        )
    readmes = artifact.get("readme_files")
    if not isinstance(readmes, list) or not readmes:
        raise remote.RemoteError(
            "artifact_inspection_required", "No inspected README is available. Run artifact inspection again."
        )
    references: dict[str, str] = {}
    for item in readmes:
        if (not isinstance(item, dict) or not _safe_path(item.get("path"))
                or not _sha256(item.get("sha256")) or item["path"] in references):
            raise remote.RemoteError(
                "invalid_readme_selection", "The inspected README list is invalid or ambiguous. Run artifact inspection again."
            )
        references[item["path"]] = item["sha256"]
    if readme_identifier == "":
        sole = next(iter(references))
        if len(references) != 1 or not _ROOT_README.fullmatch(sole):
            raise remote.RemoteError(
                "readme_selection_required", "Select the exact inspected README to review its OPatch requirement."
            )
        readme_identifier = sole
    if not _safe_path(readme_identifier) or readme_identifier not in references:
        raise remote.RemoteError(
            "invalid_readme_selection", "Select a README listed in the current artifact inspection."
        )
    alias = host.get("ssh_alias")
    if not isinstance(alias, str) or not alias.strip():
        raise remote.RemoteError("invalid_host", "The selected host has no configured SSH alias.")
    payload = remote.pull_file(
        alias, f"{artifact['path']}/{readme_identifier}", timeout=30,
        sudo=bool(host.get("sudo", False)), max_bytes=MAX_README_BYTES,
    )
    if len(payload) > MAX_README_BYTES:
        raise remote.RemoteError(
            "readme_too_large", "The selected README exceeds the 2 MiB hint limit. Review it manually."
        )
    digest = hashlib.sha256(payload).hexdigest()
    if digest != references[readme_identifier]:
        raise remote.RemoteError(
            "readme_changed", "The selected README changed after inspection. Run artifact inspection again."
        )
    version, sentence, warnings = _extract_requirement(payload)
    return {
        "artifact_sha256": artifact["sha256"],
        "readme_identifier": readme_identifier,
        "readme_sha256": digest,
        "required_opatch_version": version,
        "evidence": sentence,
        "warnings": warnings,
    }
