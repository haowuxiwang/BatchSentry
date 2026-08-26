p = "tests/unit/test_pipeline.py"
s = open(p, encoding="utf-8").read()
old = '''        async def fake_cancelled(jid):
            calls["n"] += 1
            return calls["n"] >= 6'''
new = '''        async def fake_cancelled(jid):
            calls["n"] += 1
            import sys as _s
            print(f"[cancel-call #{calls['n']}]", file=_s.stderr)
            return calls["n"] >= 6'''
assert old in s, "anchor missing"
s = s.replace(old, new, 1)
open(p, "w", encoding="utf-8", newline="").write(s)
print("instrumented")
