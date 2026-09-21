"""往存档的国家变量块里塞一个变量（默认 om_verify_boost），用于 M1-9 验收。

为什么不走控制台/游戏内操作：验证环境里游戏进程活不过工具侧的后台任务时限，
"挂机十年"不可行。改存档是唯一不碰控制台（需求 G-2）、也不需要人盯屏幕的办法。

做法是**定点文本手术**，不是全量重新序列化——后者对 80 MB 的 gamestate
做 round-trip，风险远大于收益。

定位逻辑（实测确认过，别改成更"聪明"的写法）：
  1. 行首 ``country=`` → 大括号匹配出整个 country 表
  2. 表内 ``\\n\\t1=\\n\\t{`` → 玩家国家（id 来自 gamestate 的 ``player=`` 块）
  3. 国家块内**第一个** ``variables=\\n\\t\\t{\\n`` → 国家变量块
     （注意：往后还有缩进更深的 variables，那是别的东西，不能用"第一个匹配"以外
      的规则选）

用法::

    py -3.12 tools/verify_m19_seed.py --save <path.sav> --var om_verify_boost --value 24
    py -3.12 tools/verify_m19_seed.py --newest --var om_verify_boost --value 24
    py -3.12 tools/verify_m19_seed.py --save <path.sav> --list   # 只看已有哪些变量
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SAVE_DIR = Path(
    "C:/Users/<user>/Documents/Paradox Interactive/Stellaris/save games"
)


def read_gamestate(path: Path) -> str:
    with zipfile.ZipFile(path) as z:
        return z.read("gamestate").decode("utf-8", "replace")


def find_brace_end(text: str, open_index: int) -> int:
    """从 ``open_index`` 处的 '{' 出发，返回配对 '}' 的下标。"""
    depth = 0
    for i in range(open_index, len(text)):
        c = text[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i
    raise ValueError("大括号不配对")


def player_country_id(gamestate: str) -> int:
    m = re.search(r"\nplayer=\n\{\n.*?country=(\d+)", gamestate, re.S)
    if not m:
        raise ValueError("找不到 playe → country 映射")
    return int(m.group(1))


def locate_country_block(gamestate: str, cid: int) -> tuple[int, int]:
    """返回玩家国家块在 gamestate 中的 [start, end) 绝对下标。"""
    top = re.search(r"^country=", gamestate, re.M)
    if not top:
        raise ValueError("找不到顶层 country= 表")
    table_open = gamestate.index("{", top.start())
    table_end = find_brace_end(gamestate, table_open)
    country = re.search(rf"\n\t{cid}=\n\t\{{", gamestate[table_open:table_end])
    if not country:
        raise ValueError(f"country 表里找不到 id={cid}")
    abs_start = table_open + country.start()
    entry_open = gamestate.index("{", abs_start)
    return abs_start, find_brace_end(gamestate, entry_open) + 1


def locate_variables_block(gamestate: str, cstart: int, cend: int) -> tuple[int, int]:
    """返回国家变量块 ``variables= {...}`` 的 [start, end) 绝对下标。"""
    pat = re.compile(r"variables=\n\t\t\{\n")
    m = pat.search(gamestate, cstart, cend)
    if not m:
        raise ValueError("国家块里找不到国家变量块")
    open_idx = gamestate.index("{", m.start())
    return m.start(), find_brace_end(gamestate, open_idx) + 1


def list_vars(body: str) -> list[tuple[str, str]]:
    out = []
    for m in re.finditer(r"^\t\t\t([A-Za-z_0-9]+)=(.*)$", body, re.M):
        out.append((m.group(1), m.group(2).strip()))
    return out


def set_var(gamestate: str, name: str, value: str) -> tuple[str, bool, str | None]:
    """把 ``name`` 设成 ``value``；存在则改，不存在则插。返回 (新文本, 是否新增, 旧值)。"""
    cid = player_country_id(gamestate)
    cstart, cend = locate_country_block(gamestate, cid)
    vstart, vend = locate_variables_block(gamestate, cstart, cend)
    block = gamestate[vstart:vend]

    existing = re.search(rf"^\t\t\t{re.escape(name)}=(.*)$", block, re.M)
    if existing:
        old = existing.group(1).strip()
        new_block = (
            block[: existing.start(1)] + value + block[existing.end(1) :]
        )
        return gamestate[:vstart] + new_block + gamestate[vend:], False, old

    # 插在变量块的 '{' 之后。**不能**插在 ``variables=`` 和 ``{`` 之间——
    # 那会把 key 塞进键名和开括号中间，语法直接坏掉（实测踩过）。
    brace = block.index("{")
    insert_at = block.index("\n", brace) + 1
    line = f"\t\t\t{name}={value}\n"
    new_block = block[:insert_at] + line + block[insert_at:]
    return gamestate[:vstart] + new_block + gamestate[vend:], True, None


def write_save(src: Path, gamestate: str, dst: Path) -> None:
    """把新的 gamestate 写回 zip，其余成员原样保留。

    两个必须守住的点：
      * **先把 src 全部读进内存**，再开 dst 写。src 和 dst 常常是同一个路径，
        边读边写会把文件截断（实测踩过：zipfile 直接报 BadZipFile）。
      * 用同目录的临时文件写完再 ``os.replace``，保证中途失败时原存档还在。
    """
    with zipfile.ZipFile(src) as zin:
        members = [(n, zin.read(n)) for n in zin.namelist()]

    tmp = dst.with_suffix(dst.suffix + ".tmp_write")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
        for name, blob in members:
            if name == "gamestate":
                zout.writestr(name, gamestate.encode("utf-8"))
            else:
                zout.writestr(name, blob)
    os.replace(tmp, dst)


def newest_save(save_dir: Path) -> Path:
    saves = sorted(
        save_dir.rglob("*.sav"), key=lambda p: p.stat().st_mtime, reverse=True
    )
    if not saves:
        raise SystemExit(f"{save_dir} 下没有 .sav")
    return saves[0]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--save", type=Path)
    g.add_argument("--newest", action="store_true")
    ap.add_argument("--save-dir", type=Path, default=DEFAULT_SAVE_DIR)
    ap.add_argument("--var", default="om_verify_boost")
    ap.add_argument("--value", default="24")
    ap.add_argument("--list", action="store_true", help="只列出已有变量，不修改")
    ap.add_argument("--no-backup", action="store_true")
    args = ap.parse_args(argv)

    save = args.save if args.save else newest_save(args.save_dir)
    print(f"存档: {save}")
    gamestate = read_gamestate(save)
    print(f"gamestate: {len(gamestate):,} 字符")

    cid = player_country_id(gamestate)
    cstart, cend = locate_country_block(gamestate, cid)
    print(f"玩家国家 id={cid}，块 [{cstart}, {cend})")
    vstart, vend = locate_variables_block(gamestate, cstart, cend)
    print(f"变量块 [{vstart}, {vend})，长度 {vend - vstart}")

    if args.list:
        vs = list_vars(gamestate[vstart:vend])
        print(f"共 {len(vs)} 个变量：")
        for k, v in vs:
            print(f"  {k} = {v}")
        return 0

    new_text, added, old = set_var(gamestate, args.var, args.value)
    action = f"新增 {args.var}={args.value}" if added else f"{args.var}: {old} -> {args.value}"
    print(f"改动: {action}")

    if not args.no_backup:
        backup = save.with_suffix(save.suffix + ".bak_seed")
        shutil.copy2(save, backup)
        print(f"已备份: {backup}")

    write_save(save, new_text, save)
    print(f"已写回: {save}")

    # 读回来确认
    check = read_gamestate(save)
    cstart2, cend2 = locate_country_block(check, cid)
    vstart2, vend2 = locate_variables_block(check, cstart2, cend2)
    got = dict(list_vars(check[vstart2:vend2]))
    print(f"校验: {args.var} = {got.get(args.var)!r}")
    return 0 if got.get(args.var) == args.value else 1


if __name__ == "__main__":
    sys.exit(main())
