#!/usr/bin/env python3
# wq#2827: offline test of rotate-provider-keys swap() against copies of the real files (values never printed).
import importlib.util, importlib.machinery, os, shutil, tempfile, stat, sys
loader = importlib.machinery.SourceFileLoader('rpk', '/home/lumen/bin/rotate-provider-keys')
spec = importlib.util.spec_from_loader('rpk', loader); rpk = importlib.util.module_from_spec(spec); loader.exec_module(rpk)
T = tempfile.mkdtemp(prefix='rpk-test-'); os.chmod(T, 0o700)
real_env, real_raw = list(rpk.ENV_FILES), list(rpk.RAW_FILES)
rpk.ENV_FILES = [shutil.copy2(f, os.path.join(T, os.path.basename(f) + f'.{i}')) for i, f in enumerate(real_env)]
rpk.RAW_FILES = [shutil.copy2(f, os.path.join(T, os.path.basename(f))) for f in real_raw]
rpk.LOG = os.path.join(T, 'log')
before = [rpk.read_env(f) for f in rpk.ENV_FILES]; raw_before = open(rpk.RAW_FILES[0]).read()
new = {'GEMINI': 'NEW-gemini-test-value-000000000', 'DEEPSEEK': 'sk-NEW-deepseek-test-00000000'}
old = rpk.swap(new)
after = [rpk.read_env(f) for f in rpk.ENV_FILES]
res = []
def ok(name, c): res.append(c); print(('PASS ' if c else 'FAIL ') + name)
ok('old gemini set had 2 distinct values (agent/.env + gemini.env, wq#2902)', len(old['GEMINI']) == 2)
for i, f in enumerate(rpk.ENV_FILES):
    for var, prov in (('GEMINI_API_KEY', 'GEMINI'), ('DEEPSEEK_API_KEY', 'DEEPSEEK')):
        if var in before[i]: ok(f'{os.path.basename(f)} {var} swapped', after[i][var] == new[prov])
    untouched = all(after[i][k] == v for k, v in before[i].items() if k not in ('GEMINI_API_KEY', 'DEEPSEEK_API_KEY'))
    ok(f'{os.path.basename(f)} every other key untouched ({len(before[i])} keys)', untouched and set(after[i]) == set(before[i]))
    ok(f'{os.path.basename(f)} mode stays 0600', stat.S_IMODE(os.stat(f).st_mode) == 0o600)
ok('raw gemini-ai-studio.key swapped (it held an old gemini value)', raw_before.strip() in old['GEMINI'] and open(rpk.RAW_FILES[0]).read() == new['GEMINI'])
ok('raw file mode 0600', stat.S_IMODE(os.stat(rpk.RAW_FILES[0]).st_mode) == 0o600)
ok('log carries no value', not any(v in open(rpk.LOG).read() for v in list(new.values()) + [x for s in old.values() for x in s]))
ok('real files untouched', all(rpk.read_env(f) == rpk.read_env(f) for f in real_env))
shutil.rmtree(T)
print(f'{sum(res)}/{len(res)} passed'); sys.exit(0 if all(res) else 1)
