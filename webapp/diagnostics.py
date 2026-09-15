"""Bounded redaction shared by persisted diagnostics and downloadable reports."""
import re

_CREDENTIAL_KEY = r'(?:[A-Za-z0-9_.-]*[_-])?(?:password|passwd|pwd|token|secret|api[_-]?key|access[_-]?key)'
_KEY_VALUE = re.compile(r'''(?ix)((?<![A-Za-z0-9_])''' + _CREDENTIAL_KEY + r'''(?![A-Za-z0-9_])["']?\s*(?:[:=]\s*|\s+))
                            (?:"[^"]*"|'[^']*'|[^\s,;]+)''')


def redact_text(value, limit=16000):
    text = str(value or '')
    text = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', text)
    text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', text)
    text = re.sub(r'(?i)(\b(?:Bearer|Basic)\s+)[^\s,"\']+', r'\1[REDACTED]', text)
    text = re.sub(r'''(?im)(\b(?:proxy-)?authorization["']?\s*:\s*["']?)(?!\s*(?:Bearer|Basic)\b)[^\r\n"']+''', r'\1[REDACTED]', text)
    text = _KEY_VALUE.sub(r'\1[REDACTED]', text)
    text = re.sub(r'(?i)(\b[a-z][a-z0-9+.-]*://[^\s/:@]+:)[^\s/@]+(@)', r'\1[REDACTED]\2', text)
    text = re.sub(r'(?i)(\b(?:connect|conn|sqlplus)\s+(?:-[A-Za-z]+\s+)*[^\s/]+/)(?:"[^"]*"|[^\s@]+)', r'\1[REDACTED]', text)
    text = re.sub(r'-----BEGIN ([A-Z ]*PRIVATE KEY)-----.*?(?:-----END \1-----|\Z)', '[REDACTED PRIVATE KEY]', text, flags=re.S)
    return text[-limit:]


def redacted(value):
    if isinstance(value, dict):
        return {str(key): ('[REDACTED]' if re.fullmatch(_CREDENTIAL_KEY, str(key), re.I) or re.fullmatch(r'(?:proxy-)?authorization', str(key), re.I)
                          else redacted(item)) for key, item in value.items()}
    if isinstance(value, list):
        return [redacted(item) for item in value]
    return redact_text(value) if isinstance(value, str) else value
