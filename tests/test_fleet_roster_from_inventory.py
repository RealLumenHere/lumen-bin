"""wq#1456 — the ops scripts take their node lists from the fleet inventory, never from a
NODES/HOSTS literal. Every script here is exercised against a TEMP inventory root, so the
assertions are about derivation rules (monitored / status / never_restart / crown_jewel /
self / maint_tier), not about today's fleet.

Needs the inventory code (fleet_inventory.py + fleet-inv with ssh-list filters, fleet-inventory
PR for wq#1456). $FLEET_INVENTORY_DIR points at it (default /home/lumen/fleet/inventory);
skipped if that checkout predates the filters."""
import importlib.machinery
import importlib.util
import os
import subprocess
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
BIN = os.path.dirname(HERE)
INV_DIR = os.environ.get("FLEET_INVENTORY_DIR", "/home/lumen/fleet/inventory")
FLEET_INV = os.path.join(INV_DIR, "fleet-inv")

sys.path.insert(0, INV_DIR)
try:
    import fleet_inventory as inv
except ImportError:  # pragma: no cover
    inv = None

pytestmark = pytest.mark.skipif(inv is None or not hasattr(inv, "maintenance_targets"),
                                reason=f"fleet_inventory at {INV_DIR} lacks the wq#1456 selectors")


def _rec(name, status="active", monitor=True, ts="100.64.0.1", user=None, roles=("worker",),
         tier=None, crown=False, never_restart=False, plug=None, key=None, method="key"):
    user = name if user is None else user
    ops = "ops: {known_issues: []%s}\n" % (f", maint_tier: {tier}" if tier is not None else "")
    ssh = f"  ssh: {{user: {user}, method: {method}%s}}\n" % (f", key: {key}" if key else "")
    plug_s = f"hardware: {{power_hw: {{plug_id: {plug}}}}}\n" if plug else "hardware: {}\n"
    decom = "\n  decommissioned: {date: 2026-08-22, reason: retired}" if status == "decommissioned" else ""
    return (f"identity: {{name: {name}, aliases: [], make_model: box-{name}, serial: VERIFY, location: rack}}\n"
            f"status:\n  status: {status}\n  power: on\n  monitor: {str(monitor).lower()}{decom}\n"
            f"network:\n  tailscale_ip: {ts}\n  lan_ip: 192.168.8.{ts.split('.')[-1]}\n{ssh}  nics: []\n"
            f"{plug_s}roles: [{', '.join(roles)}]\nservices: []\nserves: []\n"
            f"governance: {{crown_jewel: {str(crown).lower()}, never_restart: {str(never_restart).lower()}, off_limits: false}}\n"
            f"{ops}")


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A 9-box fleet: the shapes every script must get right."""
    m = tmp_path / "machines"
    m.mkdir()
    (m / "hub.yaml").write_text(_rec("hub", ts="100.64.0.1", tier=5, crown=True, plug="P11", roles=("orchestrator-host",)))
    (m / "vault.yaml").write_text(_rec("vault", ts="100.64.0.2", user="vaultadm", tier=4, crown=True, plug="P4"))
    (m / "leaf.yaml").write_text(_rec("leaf", ts="100.64.0.3", tier=1, plug="P6", key="/tmp/leaf_key"))
    (m / "serv.yaml").write_text(_rec("serv", ts="100.64.0.4", tier=2))
    (m / "infer.yaml").write_text(_rec("infer", ts="100.64.0.5", tier=1.5, roles=("inference",)))
    (m / "laptop.yaml").write_text(_rec("laptop", ts="100.64.0.6", never_restart=True))
    (m / "parked.yaml").write_text(_rec("parked", "standby", monitor=False, ts="100.64.0.7", tier=3))
    (m / "gone.yaml").write_text(_rec("gone", "decommissioned", monitor=False, ts="100.64.0.8", method="n/a", plug="P2"))
    (m / "benchbox.yaml").write_text(_rec("benchbox", "bench", ts="100.64.0.9"))
    for k, v in {"FLEET_INVENTORY_ROOT": str(tmp_path), "FLEET_INVENTORY_DIR": INV_DIR, "FLEET_INV": FLEET_INV,
                 "FLEET_INV_NO_AQ": "1", "FLEET_INV_HOSTNAME": "hub"}.items():
        monkeypatch.setenv(k, v)
    return tmp_path


def _load(script):
    """Import a bin/ script (no .py suffix) as a module; its top level derives the roster."""
    path = os.path.join(BIN, script)
    loader = importlib.machinery.SourceFileLoader(script.replace("-", "_"), path)
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def _sh(script, *args):
    p = subprocess.run(["bash", os.path.join(BIN, script), *args], capture_output=True, text=True)
    return p.returncode, p.stdout, p.stderr


def test_fleet_update_check_nodes(env):
    m = _load("fleet-update-check")
    # maintenance roster: monitored + active + not never_restart, self = "" (local), maint order
    assert list(m.NODES.items()) == [("leaf", "leaf@100.64.0.3"), ("infer", "infer@100.64.0.5"),
                                     ("serv", "serv@100.64.0.4"), ("vault", "vaultadm@100.64.0.2"),
                                     ("hub", "")]
    for absent in ("laptop", "parked", "gone", "benchbox"):
        assert absent not in m.NODES
    assert m._ollama_nodes() == [("hub", "127.0.0.1"), ("infer", "100.64.0.5")]


def test_fleet_maint_assemble_order_and_absent(env):
    m = _load("fleet-maint-assemble")
    assert m.NODE_ORDER == ["leaf", "infer", "serv", "vault", "hub"]
    assert m.DECLARED_ABSENT_DEFAULT == {"parked", "benchbox"}
    assert m.node_tier("vault") == (4.0, "vault") and m.node_tier("infer") == (1.5, "infer")
    assert m.node_tier("stranger") == (6.0, "stranger")  # unlisted-but-reachable sorts after lumen


def test_fleet_maint_run_order(env):
    m = _load("fleet-maint-run")
    assert m.NODE_ORDER == ["leaf", "infer", "serv", "vault", "hub"]
    problems = m.validate_batch([{"id": 1, "node": "gone", "apply_cmd": "x", "health_probe_cmd": "y",
                                  "pre_state_cmd": "a", "rollback_cmd": "b", "rollback_dryrun_cmd": "c"}])
    assert any("no tier" in p for p in problems)  # a decommissioned node is refused, never run


def test_drift_assert_hosts_and_skips(env):
    m = _load("drift-assert")
    assert m.HOSTS == {"hub": "LOCAL", "vault": "vaultadm@100.64.0.2", "leaf": "leaf@100.64.0.3",
                       "serv": "serv@100.64.0.4", "infer": "infer@100.64.0.5", "laptop": "laptop@100.64.0.6",
                       "parked": "parked@100.64.0.7", "benchbox": "benchbox@100.64.0.9"}
    assert m.SKIP_HOSTS == {"parked"}
    assert "gone" not in m.HOSTS
    assert not any(h == "nephthys" for _, h, *_ in m.GOLDEN)


def test_ui_inventory_nodes(env):
    m = _load("ui-inventory")
    nodes = m.discover_nodes()
    assert sorted(nodes) == ["benchbox", "hub", "infer", "laptop", "leaf", "serv", "vault"]
    assert nodes["vault"] == {"tailscale_ip": "100.64.0.2", "lan_ip": "192.168.8.2", "role": "worker",
                              "status": "active", "hardware": "box-vault"}


def test_fleet_keymesh_list(env):
    rc, out, err = _sh("fleet-keymesh", "--list")
    assert rc == 0, err
    # every live-status box with an ssh path, parked standby included, decommissioned excluded
    assert out.split("\n")[:-1] == ["benchbox\tbenchbox@100.64.0.9", "hub\thub@100.64.0.1", "infer\tinfer@100.64.0.5",
                                    "laptop\tlaptop@100.64.0.6", "leaf\tleaf@100.64.0.3", "parked\tparked@100.64.0.7",
                                    "serv\tserv@100.64.0.4", "vault\tvaultadm@100.64.0.2"]


def test_fleet_roll_reboot_list(env):
    rc, out, err = _sh("fleet-roll-reboot.sh", "--list")
    assert rc == 0, err
    assert out.split("\n")[:-1] == ["infer\tinfer@100.64.0.5", "leaf\tleaf@100.64.0.3", "serv\tserv@100.64.0.4"]
    for excluded in ("hub", "vault", "laptop", "parked", "benchbox", "gone"):
        assert excluded not in out


def test_rotate_fleet_pw_list(env):
    rc, out, err = _sh("rotate-fleet-pw.sh", "--list")
    assert rc == 0, err
    rows = out.split("\n")[:-1]
    assert rows == ["leaf|leaf|100.64.0.3|/tmp/leaf_key", "infer|infer|100.64.0.5|/home/lumen/.ssh/id_ed25519",
                    "serv|serv|100.64.0.4|/home/lumen/.ssh/id_ed25519", "parked|parked|100.64.0.7|/home/lumen/.ssh/id_ed25519",
                    "vault|vaultadm|100.64.0.2|/home/lumen/.ssh/id_ed25519", "hub|hub|LOCAL|/home/lumen/.ssh/id_ed25519",
                    "laptop|laptop|100.64.0.6|/home/lumen/.ssh/id_ed25519"]
    rc, out, _ = _sh("rotate-fleet-pw.sh", "--include-bench", "--list")
    assert rc == 0 and "benchbox|benchbox|100.64.0.9|" in out


def test_scripts_refuse_without_roster(env, monkeypatch, tmp_path):
    empty = tmp_path / "empty"
    (empty / "machines").mkdir(parents=True)
    monkeypatch.setenv("FLEET_INVENTORY_ROOT", str(empty))
    for script in ("fleet-keymesh", "fleet-roll-reboot.sh", "rotate-fleet-pw.sh"):
        rc, out, err = _sh(script, "--list")
        assert rc == 1 and "empty roster" in (out + err), script


def test_bash_scripts_parse():
    for script in ("fleet-keymesh", "fleet-roll-reboot.sh", "rotate-fleet-pw.sh", "plug-ctl"):
        p = subprocess.run(["bash", "-n", os.path.join(BIN, script)], capture_output=True, text=True)
        assert p.returncode == 0, p.stderr
