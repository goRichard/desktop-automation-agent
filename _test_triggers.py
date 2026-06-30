import sys
sys.stdout.reconfigure(encoding='utf-8')
sys.path.insert(0, 'c:/Users/z00490ns/Code/desktop-agent')
from pathlib import Path
import yaml

raw = Path('c:/Users/z00490ns/Code/desktop-agent/skills/user_skills/send_outlook_email/SKILL.md').read_text(encoding='utf-8')
parts = raw.split('---', 2)
print('parts count:', len(parts))
print('--- frontmatter ---')
print(repr(parts[1][:300]))
print()
meta = yaml.safe_load(parts[1]) or {}
print('meta:', meta)
print('triggers raw:', meta.get('triggers'))
