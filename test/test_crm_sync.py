import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def crm(monkeypatch):
    fake_models = ModuleType("models")
    fake_models.db = MagicMock()
    fake_batch_apply = ModuleType("batch_apply")
    fake_batch_apply.apply_snapshot = MagicMock(return_value={"created": 1, "updated": 0})
    monkeypatch.setitem(sys.modules, "models", fake_models)
    monkeypatch.setitem(sys.modules, "batch_apply", fake_batch_apply)

    path = Path(__file__).resolve().parents[1] / "crm_sync.py"
    spec = importlib.util.spec_from_file_location("crm_sync_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, fake_models.db, fake_batch_apply.apply_snapshot


def make_customer(login="ivan", robot=False, nds=False):
    return SimpleNamespace(
        id=101,
        name="Покупатель",
        custom_fields_values={
            "Логин": {"value": login},
            "Робот": {"value": robot},
            "Оплата с НДС": {"value": nds},
        },
        contacts=[],
        companies=[],
        _embedded={},
    )


def test_customer_boolean_fields_false(crm):
    module, _, _ = crm
    row = module.customer_row(make_customer(robot=False, nds=False), "test-run")
    assert row["payload"]["general"]["is_robot"] is False
    assert row["payload"]["general"]["is_nds"] is False


def test_customer_boolean_fields_true(crm):
    module, _, _ = crm
    row = module.customer_row(make_customer(robot=True, nds=True), "test-run")
    assert row["payload"]["general"]["is_robot"] is True
    assert row["payload"]["general"]["is_nds"] is True


def test_customer_without_login_is_skipped(crm):
    module, _, _ = crm
    assert module.customer_row(make_customer(login=None), "test-run") is None


def test_lead_without_login_is_allowed(crm):
    module, _, _ = crm
    lead = SimpleNamespace(id=200, name="Сделка", custom_fields_values={}, contacts=[])
    row = module.lead_row(lead, "test-run")
    assert row["crm_id"] == 200
    assert row["login"] is None


def test_insert_batch_uses_last_duplicate_crm_id(crm):
    module, db, _ = crm
    connection = db.engine.begin.return_value.__enter__.return_value
    rows = [
        {"run_id": "run-1", "crm_id": 101, "login": "ivan"},
        {"run_id": "run-1", "crm_id": 101, "login": "petr"},
        {"run_id": "run-1", "crm_id": 102, "login": "maria"},
    ]
    module._insert_batch(module._CUSTOMERS, rows)
    statement = connection.execute.call_args.args[0]
    inserted = statement._multi_values[0]
    assert len(inserted) == 2
    assert inserted[0]["login"] == "petr"
    assert inserted[1]["login"] == "maria"


def test_apply_only_after_collection_finishes(crm, monkeypatch):
    module, db, apply_snapshot = crm
    monkeypatch.setattr(module, "_stage", MagicMock(side_effect=[2, 3]))
    result = module.sync_crm([], [], is_full=True)
    assert result["stats"] == {"created": 1, "updated": 0}
    assert module._stage.call_count == 2
    apply_snapshot.assert_called_once()
    db.session.expire_all.assert_called_once()


def test_collection_failure_prevents_apply(crm, monkeypatch):
    module, db, apply_snapshot = crm
    monkeypatch.setattr(module, "_stage", MagicMock(side_effect=RuntimeError("CRM unavailable")))
    with pytest.raises(RuntimeError, match="CRM unavailable"):
        module.sync_crm([], [], is_full=True)
    apply_snapshot.assert_not_called()
    statements = [
        str(call.args[0])
        for call in db.engine.begin.return_value.__enter__.return_value.execute.call_args_list
    ]
    assert any("status='failed'" in sql for sql in statements)


def test_invalid_batch_size_does_not_start_sync(crm):
    module, db, _ = crm
    with pytest.raises(ValueError, match="batch_size must be positive"):
        module.sync_crm([], [], batch_size=0)
    db.engine.begin.assert_not_called()
