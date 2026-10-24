"""Delivery cues: how a line of the reply is to be said, e.g. `[shyly] えへへ` (ADR-0007).

A cue as Fish Audio reads it is English in square brackets. Drama 3 also reads paired
tags around the words they are for (`<whisper>内緒だよ</whisper>`), from a few it knows
(`PAIRED_TAGS`); voiced with any other model, they are left out (`spoken_by`). The reply
keeps what the model wrote, but only speech synthesis hears the cues, and only
well-formed ones: what looks like a cue written some other way (`(happy)`, `（小声で）`,
`【嬉しそうに】`, `[嬉しそうに]`, `<|speaker:0|>`) is dropped for everyone rather than read
out as words. What people see, and what the background models read, has no cues at all.
"""

import re
from dataclasses import dataclass

from eidolon.config import FishAudioModel

PAIRED_TAGS = ("whisper", "emphasis", "soft", "fast", "slow", "stress", "forceful")
"""Drama 3's paired tags for how the words between are said; not its tags for the
language, which the replies have no use for."""
TAG_READERS: frozenset[FishAudioModel] = frozenset({"drama-3"})
"""The models that read paired tags."""

_CLOSERS = {"[": "]", "(": ")）", "（": ")）", "【": "】", "<": ">"}
_CUE = re.compile(r"\[[A-Za-z][A-Za-z ,'\-]*\]")
_TAG = re.compile(rf"<(/?)(?:{'|'.join(PAIRED_TAGS)})>")
LONGEST_CUE = 80
"""An opening bracket not closed within this many characters is not a cue but part of
the line, and goes on as written rather than holding the line back."""


@dataclass
class CueFilter:
    """Splits a line streamed in chunks into what is shown and what is voiced.

    A bracket is held back until it closes, so nobody sees half a cue before it goes.
    What is voiced goes out only along with something shown, so a cue with no words
    after it in the line is never voiced; a closing tag, once some of the line has been,
    goes out with the words it closes on. Whitespace is held until more of the line
    follows, so what is shown neither starts nor ends with it."""

    _held: str = ""  # from an opening bracket not yet closed
    _shown_any: bool = False
    _space: str = ""  # whitespace yet to be shown
    _voiced: str = ""  # cues and whitespace yet to be voiced

    def feed(self, chunk: str) -> tuple[str, str]:
        """What of the line so far can be shown, and what voiced, beyond what was."""
        self._held += chunk
        shown = voiced = ""
        while self._held:
            opener = min((i for c in _CLOSERS if (i := self._held.find(c)) >= 0), default=-1)
            if opener < 0:
                text, self._held = self._held, ""
            elif opener > 0:
                text, self._held = self._held[:opener], self._held[opener:]
            else:
                close = min(
                    (i for c in _CLOSERS[self._held[0]] if (i := self._held.find(c, 1)) >= 0),
                    default=-1,
                )
                if close < 0:
                    if len(self._held) <= LONGEST_CUE:
                        break  # wait for it to close
                    text, self._held = self._held[0], self._held[1:]
                else:
                    bracketed, self._held = self._held[: close + 1], self._held[close + 1 :]
                    tag = _TAG.fullmatch(bracketed)
                    if tag is not None and tag[1]:  # closes on the words before it
                        if self._shown_any:
                            voiced += bracketed
                    elif tag is not None or _CUE.fullmatch(bracketed):
                        self._voiced += bracketed
                    continue
            more_shown, more_voiced = self._take(text)
            shown += more_shown
            voiced += more_voiced
        return shown, voiced

    def finish(self) -> tuple[str, str]:
        """The line is over: what is left of it, a bracket that never closed as written."""
        held, self._held = self._held, ""
        shown, voiced = self._take(held)
        self._space = self._voiced = ""
        return shown, voiced

    def _take(self, text: str) -> tuple[str, str]:
        if not self._shown_any:
            text = text.lstrip()
        body = text.rstrip()
        if not body:
            self._space += text
            self._voiced += text
            return "", ""
        shown = self._space + body
        voiced = self._voiced + text[: len(body)]
        self._shown_any = True
        self._space = self._voiced = text[len(body) :]
        return shown, voiced


def without_cues(text: str) -> str:
    """`text` as people see it."""
    line = CueFilter()
    shown, _ = line.feed(text)
    return shown + line.finish()[0]


_ACTION = re.compile(r"[（(][^（()）]{1,40}[）)]")


def voiced(text: str) -> str:
    """`text` as speech synthesis hears it: what people see, with its well-formed cues, and
    without the actions she wrote in round brackets, which are not said aloud."""
    line = CueFilter()
    _, said = line.feed(text)
    return _ACTION.sub("", said + line.finish()[1])


def spoken_by(voiced: str, model: FishAudioModel) -> str:
    """What is `voiced`, as `model` is given it: without paired tags, unless it reads them.
    Each tag in `voiced` is whole, as `CueFilter` voices them."""
    return voiced if model in TAG_READERS else _TAG.sub("", voiced)
