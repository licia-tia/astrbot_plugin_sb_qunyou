"""PluginConfig 单元测试。"""

import pytest

from astrbot_plugin_sb_qunyou.config import DatabaseConfig, PluginConfig


class TestDefaults:
    def test_default_debounce_mode(self):
        cfg = PluginConfig()
        assert cfg.debounce.mode == "time"

    def test_default_topic_threshold(self):
        cfg = PluginConfig()
        assert 0 < cfg.topic.similarity_threshold < 1

    def test_default_emotion_disabled(self):
        cfg = PluginConfig()
        assert cfg.emotion.enabled is True

    def test_default_jargon_min_frequency(self):
        cfg = PluginConfig()
        assert cfg.jargon.min_frequency >= 1

    def test_default_webui_port(self):
        cfg = PluginConfig()
        assert cfg.webui.port > 0

    def test_default_review_gate_disabled(self):
        cfg = PluginConfig()
        assert cfg.review_gate.enabled_for_group_persona is False
        assert cfg.review_gate.enabled_for_tone is False

    def test_default_retrieval_only_query_preferred(self):
        cfg = PluginConfig()
        assert cfg.knowledge.retrieval_only_query_preferred is True

    def test_default_database_connection_url_requires_conf(self):
        cfg = PluginConfig()
        with pytest.raises(ValueError, match="Database configuration is incomplete"):
            cfg.database.connection_url()

    def test_database_pool_options(self):
        db_cfg = DatabaseConfig(pool_size=10, pool_min_size=2)
        assert db_cfg.sqlalchemy_pool_options() == {
            "pool_size": 2,
            "max_overflow": 8,
        }


class TestFromAstrbotConfig:
    def test_empty_dict(self):
        cfg = PluginConfig.from_astrbot_config({})
        assert cfg.debounce.mode == "time"  # default

    def test_maps_flat_keys(self):
        raw = {
            "Debounce_Settings": {
                "mode": "off",
                "time_window_seconds": 5.0,
            },
            "Topic_Settings": {
                "enabled": False,
            },
            "WebUI_Settings": {
                "port": 9090,
            },
        }
        cfg = PluginConfig.from_astrbot_config(raw)
        assert cfg.debounce.mode == "off"
        assert cfg.debounce.time_window_seconds == 5.0
        assert cfg.topic.enabled is False
        assert cfg.webui.port == 9090

    def test_partial_config(self):
        raw = {"Emotion_Settings": {"sensitivity": 0.8}}
        cfg = PluginConfig.from_astrbot_config(raw)
        assert cfg.emotion.sensitivity == 0.8
        assert cfg.debounce.mode == "time"  # unchanged default

    def test_review_gate_mapping(self):
        raw = {
            "ReviewGate_Settings": {
                "enabled_for_group_persona": True,
                "enabled_for_tone": True,
                "max_pending_per_group": 2,
            },
            "Knowledge_Settings": {
                "retrieval_only_query_preferred": False,
                "warmup_active_groups_limit": 12,
            },
        }
        cfg = PluginConfig.from_astrbot_config(raw)
        assert cfg.review_gate.enabled_for_group_persona is True
        assert cfg.review_gate.enabled_for_tone is True
        assert cfg.review_gate.max_pending_per_group == 2
        assert cfg.knowledge.retrieval_only_query_preferred is False
        assert cfg.knowledge.warmup_active_groups_limit == 12

    def test_database_settings_mapping(self):
        raw = {
            "Database_Settings": {
                "host": "db.internal",
                "port": 5433,
                "user": "qunyou",
                "password": "secret",
                "database_name": "qunyou_prod",
                "pool_size": 12,
                "pool_min_size": 4,
                "echo": True,
            }
        }
        cfg = PluginConfig.from_astrbot_config(raw)
        assert cfg.database.host == "db.internal"
        assert cfg.database.port == 5433
        assert cfg.database.user == "qunyou"
        assert cfg.database.password == "secret"
        assert cfg.database.database_name == "qunyou_prod"
        assert cfg.database.echo is True
        assert (
            cfg.database.connection_url()
            == "postgresql+asyncpg://qunyou:secret@db.internal:5433/qunyou_prod"
        )
        assert cfg.database.sqlalchemy_pool_options() == {
            "pool_size": 4,
            "max_overflow": 8,
        }

    def test_database_dsn_override_wins(self):
        raw = {
            "Database_Settings": {
                "dsn": "postgresql+asyncpg://postgres:override@db.example:5440/custom",
                "host": "ignored-host",
                "port": 9999,
                "user": "ignored-user",
                "password": "ignored-pass",
                "database_name": "ignored-db",
            }
        }
        cfg = PluginConfig.from_astrbot_config(raw)
        assert (
            cfg.database.connection_url()
            == "postgresql+asyncpg://postgres:override@db.example:5440/custom"
        )
