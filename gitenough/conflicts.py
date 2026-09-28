"""Split a file with conflict markers into blocks, and compose the resolved file from per-block choices."""

from dataclasses import dataclass, field

# Per-block choices. The labels shown to the user are branch names, never ours/theirs.
LEFT, RIGHT, LEFT_RIGHT, RIGHT_LEFT, NONE, CUSTOM = "left", "right", "left+right", "right+left", "none", "custom"


@dataclass
class Block:
    """Either common text (conflict=False) or one conflict: left = git's "ours", right = git's "theirs"."""
    conflict: bool
    lines: list[str] = field(default_factory=list)  # common text
    left: list[str] = field(default_factory=list)
    right: list[str] = field(default_factory=list)
    base: list[str] = field(default_factory=list)  # common ancestor (diff3 style), when present
    choice: str = ""  # "" while unresolved
    custom: list[str] = field(default_factory=list)

    def result(self) -> list[str]:
        if not self.conflict:
            return self.lines
        return {LEFT: self.left, RIGHT: self.right, LEFT_RIGHT: self.left + self.right,
                RIGHT_LEFT: self.right + self.left, NONE: [], CUSTOM: self.custom}.get(self.choice, [])


@dataclass
class ConflictFile:
    blocks: list[Block]
    newline: str  # "\r\n" or "\n", as found in the file
    final_newline: bool
    encoding: str

    @property
    def conflicts(self) -> list[Block]:
        return [b for b in self.blocks if b.conflict]

    def unresolved(self) -> int:
        return sum(1 for b in self.conflicts if not b.choice)

    def compose(self) -> str:
        lines = [line for b in self.blocks for line in b.result()]
        return self.newline.join(lines) + (self.newline if self.final_newline and lines else "")


def _marker(line: str, char: str) -> bool:
    return line.startswith(char * 7) and (len(line) == 7 or line[7] == " ")


def parse(raw: bytes) -> ConflictFile:
    """Blocks of a file containing <<<<<<< / ||||||| / ======= / >>>>>>> markers."""
    try:
        text, encoding = raw.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        # Keep undecodable bytes intact so they are written back exactly.
        text, encoding = raw.decode("utf-8", errors="surrogateescape"), "utf-8-surrogate"
    newline = "\r\n" if "\r\n" in text else "\n"
    final_newline = text.endswith(("\n", "\r"))
    lines = text.replace("\r\n", "\n").split("\n")
    if final_newline:
        lines.pop()
    blocks: list[Block] = []
    common: list[str] = []
    state, current = None, None
    for line in lines:
        if state is None and _marker(line, "<"):
            if common:
                blocks.append(Block(False, common))
                common = []
            current, state = Block(True), "left"
        elif state in ("left", "base") and _marker(line, "|"):
            state = "base"
        elif state in ("left", "base") and line.startswith("=======") and line.strip() == "=======":
            state = "right"
        elif state == "right" and _marker(line, ">"):
            blocks.append(current)
            current, state = None, None
        elif state == "left":
            current.left.append(line)
        elif state == "base":
            current.base.append(line)
        elif state == "right":
            current.right.append(line)
        else:
            common.append(line)
    if current is not None:  # Unterminated conflict: keep it as text rather than lose lines.
        common = ["<<<<<<<"] + current.left + ["======="] + current.right + common
    if common:
        blocks.append(Block(False, common))
    return ConflictFile(blocks, newline, final_newline, encoding)


def encode(cf: ConflictFile, text: str) -> bytes:
    if cf.encoding == "utf-8-surrogate":
        return text.encode("utf-8", errors="surrogateescape")
    return text.encode("utf-8")
