#!/usr/bin/env python
"""Build the `alaa-minimax` Ollama model: MiniMax + Alaa's playbook + project memory.

Run it again whenever core.md or the Claude memory changes, so the model stays current:
    python build.py                 # base minimax-m3.1-flash:cloud (free plan)
    python build.py minimax-m3:cloud  # needs Ollama cloud credit
"""
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
HOME = os.path.expanduser("~")
MEMORY_DIR = os.path.join(HOME, ".claude/projects/-data-data-com-termux-files-home/memory")
NAME = "alaa-minimax"
BASE = sys.argv[1] if len(sys.argv) > 1 else "minimax-m3.1-flash:cloud"


def memory_index():
    path = os.path.join(MEMORY_DIR, "MEMORY.md")
    if not os.path.exists(path):
        return ""
    lines = []
    for line in open(path, encoding="utf-8"):
        m = re.match(r"- \[(.+?)\]\((.+?)\) — (.+)", line.strip())
        if m:
            title, fname, hook = m.groups()
            lines.append(f"- {title}: {hook}  (full notes: {os.path.join(MEMORY_DIR, fname)})")
    return ("\n\n# What you already know (project memory)\n"
            "One line per topic. Before working on any of these, read_file its full notes "
            "and treat them as the latest state. Figures there are his decisions.\n"
            + "\n".join(lines))


def main():
    system = open(os.path.join(HERE, "core.md"), encoding="utf-8").read().strip()
    system += memory_index()
    if '"""' in system:
        sys.exit('core.md or memory contains """, which breaks the Modelfile')
    modelfile = os.path.join(HERE, "Modelfile")
    with open(modelfile, "w", encoding="utf-8") as f:
        f.write(f"FROM {BASE}\n")
        f.write("PARAMETER temperature 0.4\n")
        f.write(f'SYSTEM """{system}\n"""\n')
    print(f"System prompt: {len(system)} chars (~{len(system) // 4} tokens), base {BASE}")
    subprocess.run(["ollama", "create", NAME, "-f", modelfile], check=True)
    print(f"Built {NAME}. Try: ollama run {NAME}")


if __name__ == "__main__":
    main()
