"""`voicecrm crm-add`: puts a live participant into the local CRM simulator so a Teams call resolves to an account."""
from __future__ import annotations

import pytest

from voicecrm import cli
from voicecrm.adapters.crm.local_sim import LocalCrm
from voicecrm.config import Settings
from voicecrm.data_loader import load_crm_seed


@pytest.fixture()
def settings(tmp_path, monkeypatch) -> Settings:
    s = Settings(_env_file=None, state_dir=tmp_path / "state", crm_mode="local")
    LocalCrm(s.crm_db_path, load_crm_seed(s.data_dir)).close()
    monkeypatch.setattr(cli, "get_settings", lambda: s)
    return s


def rows(settings: Settings, sql: str, *params) -> list[dict]:
    crm = LocalCrm(settings.crm_db_path)
    try:
        return [dict(r) for r in crm._query(sql, params)]
    finally:
        crm.close()


def test_a_contact_is_added_to_an_existing_account_by_name(settings, capsys):
    account = rows(settings, "SELECT * FROM accounts ORDER BY name LIMIT 1")[0]
    code = cli.main(["crm-add", "--email", "Pat.Buyer@Contoso.example", "--name", "Pat Buyer", "--account", account["name"].upper()])
    assert code == 0 and "added Pat Buyer" in capsys.readouterr().out
    (contact,) = rows(settings, "SELECT * FROM contacts WHERE lower(email) = ?", "pat.buyer@contoso.example")
    assert contact["account_id"] == account["id"] and contact["name"] == "Pat Buyer"
    assert len(rows(settings, "SELECT * FROM accounts WHERE id = ?", account["id"])) == 1


def test_an_unknown_account_is_created_and_the_command_is_repeatable(settings, capsys):
    args = ["crm-add", "--email", "dana@northwind.example", "--name", "Dana", "--account", "Northwind Capital", "--phone", "206-555-0100"]
    assert cli.main(args) == 0 and "created account Northwind Capital" in capsys.readouterr().out
    assert cli.main(args) == 0                                        # idempotent: replaces, does not duplicate
    (account,) = rows(settings, "SELECT * FROM accounts WHERE name = ?", "Northwind Capital")
    (contact,) = rows(settings, "SELECT * FROM contacts WHERE email = ?", "dana@northwind.example")
    assert contact["account_id"] == account["id"] and contact["phone"] == "206-555-0100" and account["domain"] == "northwind.example"


def test_it_refuses_to_touch_a_real_crm(settings, monkeypatch, capsys):
    live = Settings(_env_file=None, state_dir=settings.state_dir, crm_mode="dataverse", dataverse_url="https://x.crm.dynamics.com")
    monkeypatch.setattr(cli, "get_settings", lambda: live)
    assert cli.main(["crm-add", "--email", "a@b.example", "--name", "A", "--account", "B"]) == 2
    assert "local CRM simulator" in capsys.readouterr().out
