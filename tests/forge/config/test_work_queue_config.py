"""The three queue settings the work-queue lane adds (contracts 6, 7 and 8).

``QueueConfig`` is ``extra="forbid"``, so adding settings has to be done the
safe way: OPTIONAL fields whose defaults reproduce exactly what the forge does
today. The first test class is the one that matters — a forge.yaml written
before this lane must still load, and still mean what it meant.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from forge.config import ForgeConfig, QueueConfig, load_config

_MINIMUM = {"permissions": {"filesystem": {"allowlist": ["/srv/forge"]}}}


def _write(tmp_path: Path, body: dict) -> Path:
    yaml_path = tmp_path / "forge.yaml"
    yaml_path.write_text(yaml.safe_dump(body))
    return yaml_path


class TestAFileWithoutThemLoadsAsToday:
    """The whole safety claim of this change, in three tests."""

    def test_a_config_with_no_queue_block_still_loads(self, tmp_path: Path) -> None:
        cfg = load_config(_write(tmp_path, dict(_MINIMUM)))
        assert isinstance(cfg, ForgeConfig)
        assert cfg.queue.max_in_flight == 1
        assert cfg.queue.order == "shadow"
        assert cfg.queue.stale_after_days == 7

    def test_a_queue_block_without_them_still_loads(self, tmp_path: Path) -> None:
        cfg = load_config(
            _write(tmp_path, {**_MINIMUM, "queue": {"default_max_turns": 7}})
        )
        assert cfg.queue.default_max_turns == 7
        assert cfg.queue.max_in_flight == 1

    def test_the_settings_that_were_already_there_are_unchanged(self) -> None:
        cfg = QueueConfig()
        assert cfg.default_max_turns == 5
        assert cfg.default_sdk_timeout_seconds == 1800
        assert cfg.default_history_limit == 50
        assert cfg.repo_allowlist == []


class TestTheThreeNewSettings:
    def test_the_fields_exist(self) -> None:
        for name in ("max_in_flight", "order", "stale_after_days"):
            assert name in QueueConfig.model_fields

    def test_the_defaults_are_todays_behaviour(self) -> None:
        cfg = QueueConfig()
        assert cfg.max_in_flight == 1
        assert cfg.order == "shadow"
        assert cfg.stale_after_days == 7

    def test_they_can_be_set_in_the_file(self, tmp_path: Path) -> None:
        cfg = load_config(
            _write(
                tmp_path,
                {
                    **_MINIMUM,
                    "queue": {
                        "max_in_flight": 2,
                        "order": "shadow",
                        "stale_after_days": 14,
                    },
                },
            )
        )
        assert cfg.queue.max_in_flight == 2
        assert cfg.queue.stale_after_days == 14

    def test_shadow_is_the_only_order_this_stage_accepts(self) -> None:
        with pytest.raises(ValidationError):
            QueueConfig(order="class")

    def test_nothing_in_flight_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            QueueConfig(max_in_flight=0)

    def test_a_staleness_threshold_of_zero_days_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            QueueConfig(stale_after_days=0)

    def test_an_unknown_queue_setting_is_still_refused(self) -> None:
        """extra='forbid' is intact — a typo is not silently accepted."""
        with pytest.raises(ValidationError):
            QueueConfig(max_in_fligt=2)
