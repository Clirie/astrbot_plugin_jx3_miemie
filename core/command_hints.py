"""Conservative command suggestions; suggestions never execute a fuzzy match."""
import re


def _one_edit_apart(left: str, right: str) -> bool:
    if abs(len(left) - len(right)) > 1:
        return False
    if len(left) == len(right):
        return sum(a != b for a, b in zip(left, right)) == 1
    short, long = sorted((left, right), key=len)
    return any(short == long[:i] + long[i + 1:] for i in range(len(long)))


def command_hint(parts: list[str], commands: dict) -> str:
    """Recognize a short command-like head, preserving normal conversation."""
    if not parts or len(parts) > 6:
        return ""
    head, *args = parts
    if head in commands or not re.fullmatch(r"[\u4e00-\u9fff]{2,8}", head):
        return ""
    families = {
        "名剑": ("名剑排行", "战绩", "名剑统计", "跨服名剑"),
        "名剑战绩": ("战绩", "名剑排行"),
    }
    candidates = [name for name in families.get(head, ()) if name in commands]
    if not candidates:
        # Exact short prefixes are useful; a one-character typo is accepted only
        # for heads of at least three characters with the same first character.
        candidates = [name for name in commands if name.startswith(head)]
        if not candidates and len(head) >= 3:
            candidates = [name for name in commands if name[0] == head[0] and _one_edit_apart(head, name)]
        candidates = candidates[:3]
    if not candidates:
        return ""
    ranking_args = head in families and 1 <= len(args) <= 2 and args[0] in {"22", "33", "55"} and (len(args) == 1 or args[1].isdigit())
    if ranking_args and "名剑排行" in candidates:
        candidates.remove("名剑排行")
        candidates.insert(0, "名剑排行")
    lines = [f"「{head}」可以用这些指令："]
    for name in candidates:
        doc = (getattr(commands[name], "__doc__", "") or "").strip().splitlines()
        usage = doc[0].strip() if doc else name
        lines.append(f"· {usage}")
    if ranking_args and "名剑排行" in candidates:
        lines.append("查看排行榜可发送：名剑排行 " + " ".join(args))
    if any(name in candidates for name in ("名剑排行", "名剑统计", "跨服名剑", "战绩")):
        lines.append("模式可填 22、33、55；个人战绩需要服务器和角色名。")
    return "\n".join(lines)
