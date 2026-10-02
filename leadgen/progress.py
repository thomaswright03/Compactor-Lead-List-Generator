"""What a running search says about how far it is.

A search reports its progress as messages: plain text, which the command line
prints as it is. Each is a Step, which also carries the values the web page's
progress bar is drawn from (web/finding.py), so the bar never depends on how a
message is worded: which step of the search it is about, and how far that step is
(done of total: Yelp or Google calls made of the calls it may make, map areas
answered of the areas asked, ...).
"""

# The steps of a search, in order (the page's STEPS names them).
LOCATE, PAID, MAP, MERGE, SAVE = "locate", "paid", "map", "merge", "save"
ORDER = (LOCATE, PAID, MAP, MERGE, SAVE)


class Step(str):
    """A progress message with its values: step (one of ORDER); done and total (0 when
    the step has no count); source ("google", "yelp") for the paid step; note, a word
    for what is going on within the step: "split" (map areas asked again in smaller
    parts), "retry" (a catch-up round for the areas the busy servers missed), "stopped"
    (searching was paused), or ""."""

    step: str
    done: int
    total: int
    source: str
    note: str

    def __new__(cls, text: str, step: str, done: int = 0, total: int = 0, source: str = "",
                note: str = "") -> "Step":
        self = super().__new__(cls, text)
        self.step, self.done, self.total, self.source, self.note = step, done, total, source, note
        return self
