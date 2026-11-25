import math
from pathlib import Path

import pytest

from arenaclub_optimizer.config import (
    APP_DIR_NAME,
    CONFIG_FILENAME,
    OVERRIDES_FILENAME,
    Config,
    ConfigError,
    Filters,
    Override,
    Overrides,
    Watch,
    config_dir,
    is_valid_topic,
    load_config,
    load_overrides,
    override_config,
    state_dir,
)
from arenaclub_optimizer.cost import PaymentMethod

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def isolated_config_home(tmp_path, monkeypatch):
    """Keep every test away from the real ~/.config."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-default"))
    monkeypatch.setenv("HOME", str(tmp_path / "home-default"))


def write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


# --- config directory -------------------------------------------------------


def test_config_dir_uses_xdg_config_home(tmp_path):
    env = {"XDG_CONFIG_HOME": str(tmp_path / "xdg")}
    assert config_dir(env, home=tmp_path / "home") == tmp_path / "xdg" / APP_DIR_NAME


@pytest.mark.parametrize("env", [{}, {"XDG_CONFIG_HOME": ""}])
def test_config_dir_falls_back_to_home_dot_config(tmp_path, env):
    assert config_dir(env, home=tmp_path) == tmp_path / ".config" / APP_DIR_NAME


def test_config_dir_reads_process_environment_by_default(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert config_dir() == tmp_path / APP_DIR_NAME


def test_state_dir_uses_xdg_state_home(tmp_path):
    env = {"XDG_STATE_HOME": str(tmp_path / "xdg")}
    assert state_dir(env, home=tmp_path / "home") == tmp_path / "xdg" / APP_DIR_NAME


@pytest.mark.parametrize(
    "env", [{}, {"XDG_STATE_HOME": ""}, {"XDG_STATE_HOME": "relative/state"}]
)
def test_state_dir_falls_back_to_home_local_state(tmp_path, env):
    assert state_dir(env, home=tmp_path) == tmp_path / ".local" / "state" / APP_DIR_NAME


def test_state_dir_reads_process_environment_by_default(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    assert state_dir() == tmp_path / APP_DIR_NAME


@pytest.mark.parametrize("topic", ["a", "Abc_123-xyz", "x" * 64])
def test_valid_topics(topic):
    assert is_valid_topic(topic)


@pytest.mark.parametrize(
    "topic", ["", "x" * 65, "has space", "slash/topic", "é", "a\n"]
)
def test_invalid_topics(topic):
    assert not is_valid_topic(topic)


def test_app_dir_and_file_names():
    assert APP_DIR_NAME == "arenaclub-optimizer"
    assert CONFIG_FILENAME == "config.toml"
    assert OVERRIDES_FILENAME == "overrides.toml"


# --- load_config: defaults --------------------------------------------------


def test_missing_file_means_all_defaults(tmp_path):
    config = load_config(tmp_path / "nope.toml")
    assert config == Config()


def test_defaults():
    config = Config()
    assert config.k == 1.0
    assert config.payment_method is PaymentMethod.wallet
    assert config.tax_rate == 0.0
    assert config.coverage_threshold == 0.95
    assert config.p_profit_threshold == 0.25
    assert config.top_heaviness_threshold == 0.5
    assert config.filters == Filters()
    assert config.watch == Watch()


def test_filter_defaults():
    filters = Filters()
    assert filters.categories == ()
    assert filters.price_min is None
    assert filters.price_max is None
    assert filters.min_ev_pct is None
    assert filters.min_p_profit is None
    assert filters.exclude_high_variance is False
    assert filters.top == 10


def test_watch_defaults():
    watch = Watch()
    assert watch.interval_minutes == 10
    assert watch.ntfy_server == "https://ntfy.sh"
    assert watch.topic is None


def test_config_is_frozen():
    with pytest.raises(AttributeError):
        Config().k = 2.0  # type: ignore[misc]


def test_empty_file_means_all_defaults(tmp_path):
    assert load_config(write(tmp_path / "c.toml", "")) == Config()


def test_default_path_is_config_toml_in_injected_directory(tmp_path):
    write(tmp_path / CONFIG_FILENAME, "k = 0.9\n")
    assert load_config(directory=tmp_path).k == 0.9


def test_default_directory_missing_means_defaults(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "empty"))
    assert load_config() == Config()


def test_load_config_does_not_create_files(tmp_path):
    load_config(directory=tmp_path / "dir")
    assert not (tmp_path / "dir").exists()


# --- load_config: values ----------------------------------------------------


def test_full_config_is_read(tmp_path):
    path = write(
        tmp_path / "c.toml",
        """
k = 0.85
payment_method = "card"
tax_rate = 0.08
coverage_threshold = 0.9
p_profit_threshold = 0.3
top_heaviness_threshold = 0.6

[filters]
categories = ["Pokemon", "Football"]
price_min = 50
price_max = 150.5
min_ev_pct = -0.05
min_p_profit = 0.2
exclude_high_variance = true
top = 5

[watch]
interval_minutes = 15
ntfy_server = "https://ntfy.example.org"
topic = "abc123_XYZ-789"
""",
    )
    config = load_config(path)
    assert config == Config(
        k=0.85,
        payment_method=PaymentMethod.card,
        tax_rate=0.08,
        coverage_threshold=0.9,
        p_profit_threshold=0.3,
        top_heaviness_threshold=0.6,
        filters=Filters(
            categories=("Pokemon", "Football"),
            price_min=50.0,
            price_max=150.5,
            min_ev_pct=-0.05,
            min_p_profit=0.2,
            exclude_high_variance=True,
            top=5,
        ),
        watch=Watch(
            interval_minutes=15,
            ntfy_server="https://ntfy.example.org",
            topic="abc123_XYZ-789",
        ),
    )
    assert isinstance(config.payment_method, PaymentMethod)
    assert isinstance(config.filters.price_min, float)


def test_partial_tables_keep_other_defaults(tmp_path):
    path = write(tmp_path / "c.toml", "[filters]\ntop = 3\n")
    config = load_config(path)
    assert config.filters == Filters(top=3)
    assert config.watch == Watch()


def test_integer_k_is_accepted_as_float(tmp_path):
    config = load_config(write(tmp_path / "c.toml", "k = 1\n"))
    assert config.k == 1.0
    assert isinstance(config.k, float)


@pytest.mark.parametrize("method", ["wallet", "ach", "card"])
def test_payment_methods(tmp_path, method):
    path = write(tmp_path / "c.toml", f'payment_method = "{method}"\n')
    assert load_config(path).payment_method is PaymentMethod(method)


def test_interval_of_exactly_five_is_allowed(tmp_path):
    path = write(tmp_path / "c.toml", "[watch]\ninterval_minutes = 5\n")
    assert load_config(path).watch.interval_minutes == 5


def test_small_k_and_boundary_thresholds_are_allowed(tmp_path):
    path = write(
        tmp_path / "c.toml",
        "k = 0.01\ncoverage_threshold = 0\np_profit_threshold = 1\n",
    )
    config = load_config(path)
    assert config.k == 0.01
    assert config.coverage_threshold == 0.0
    assert config.p_profit_threshold == 1.0


# --- load_config: errors ----------------------------------------------------


def assert_config_error(path: Path, key: str) -> None:
    with pytest.raises(ConfigError) as info:
        load_config(path)
    message = str(info.value)
    assert key in message
    assert str(path) in message


def test_config_error_is_a_value_error():
    assert issubclass(ConfigError, ValueError)


@pytest.mark.parametrize(
    ("text", "key"),
    [
        ("kk = 1.0\n", "kk"),
        ("[filters]\ncategory = []\n", "filters.category"),
        ("[watch]\ninterval = 10\n", "watch.interval"),
        ("[extra]\nx = 1\n", "extra"),
    ],
)
def test_unknown_keys_raise(tmp_path, text, key):
    assert_config_error(write(tmp_path / "c.toml", text), key)


@pytest.mark.parametrize(
    ("text", "key"),
    [
        ("k = -0.1\n", "k"),
        ("k = 0\n", "k"),
        ("k = 0.0\n", "k"),
        (f"k = {10**400}\n", "k"),
        ("k = nan\n", "k"),
        ("k = inf\n", "k"),
        ("tax_rate = 1.0\n", "tax_rate"),
        ("tax_rate = 1.5\n", "tax_rate"),
        ("tax_rate = -0.01\n", "tax_rate"),
        ("coverage_threshold = 1.01\n", "coverage_threshold"),
        ("coverage_threshold = -0.1\n", "coverage_threshold"),
        ("p_profit_threshold = 2\n", "p_profit_threshold"),
        ("top_heaviness_threshold = -1\n", "top_heaviness_threshold"),
        ("[filters]\nprice_min = -1\n", "filters.price_min"),
        ("[filters]\nprice_max = inf\n", "filters.price_max"),
        ("[filters]\nmin_ev_pct = nan\n", "filters.min_ev_pct"),
        ("[filters]\nmin_p_profit = 1.5\n", "filters.min_p_profit"),
        ("[filters]\ntop = 0\n", "filters.top"),
        ("[watch]\ninterval_minutes = 4\n", "watch.interval_minutes"),
        ("[watch]\ninterval_minutes = 4.9\n", "watch.interval_minutes"),
        ('[watch]\nntfy_server = "ntfy.sh"\n', "watch.ntfy_server"),
        ('[watch]\nntfy_server = "https://"\n', "watch.ntfy_server"),
        ('[watch]\nntfy_server = "http://"\n', "watch.ntfy_server"),
        ('[watch]\ntopic = ""\n', "watch.topic"),
        ('[watch]\ntopic = "has space"\n', "watch.topic"),
        (f'[watch]\ntopic = "{"a" * 65}"\n', "watch.topic"),
    ],
)
def test_out_of_range_values_raise(tmp_path, text, key):
    assert_config_error(write(tmp_path / "c.toml", text), key)


def test_price_min_above_price_max_raises(tmp_path):
    path = write(tmp_path / "c.toml", "[filters]\nprice_min = 200\nprice_max = 100\n")
    assert_config_error(path, "filters.price_min")


@pytest.mark.parametrize(
    ("text", "key"),
    [
        ('k = "1.0"\n', "k"),
        ("k = true\n", "k"),
        ('payment_method = "bitcoin"\n', "payment_method"),
        ("payment_method = 1\n", "payment_method"),
        ('[filters]\ncategories = "Pokemon"\n', "filters.categories"),
        ("[filters]\ncategories = [1, 2]\n", "filters.categories"),
        ('[filters]\nexclude_high_variance = "yes"\n', "filters.exclude_high_variance"),
        ("[filters]\ntop = 2.5\n", "filters.top"),
        ("[filters]\ntop = true\n", "filters.top"),
        ("[watch]\ninterval_minutes = true\n", "watch.interval_minutes"),
        ("[watch]\ntopic = 5\n", "watch.topic"),
        ("filters = 1\n", "filters"),
        ("watch = []\n", "watch"),
    ],
)
def test_wrong_types_raise(tmp_path, text, key):
    assert_config_error(write(tmp_path / "c.toml", text), key)


def test_invalid_toml_raises_config_error_naming_file(tmp_path):
    path = write(tmp_path / "c.toml", "k = = 1\n")
    with pytest.raises(ConfigError) as info:
        load_config(path)
    assert str(path) in str(info.value)


# --- load_overrides ---------------------------------------------------------


def test_missing_overrides_file_means_no_overrides(tmp_path):
    overrides = load_overrides(tmp_path / "nope.toml")
    assert overrides == Overrides()
    assert overrides.lookup("card_abc", "12345678") is None


def test_overrides_default_path_is_in_injected_directory(tmp_path):
    write(tmp_path / OVERRIDES_FILENAME, '["12345678"]\nmarket_value = 250\n')
    overrides = load_overrides(directory=tmp_path)
    assert overrides.lookup("card_x", "12345678") == 250.0


def test_overrides_default_directory_from_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    (tmp_path / APP_DIR_NAME).mkdir()
    write(
        tmp_path / APP_DIR_NAME / OVERRIDES_FILENAME,
        "[card_abc]\nmarket_value = 12.5\n",
    )
    assert load_overrides().lookup("card_abc", None) == 12.5


OVERRIDES_TEXT = """
["12345678"]
market_value = 250.0
note = "PSA 10 comps, Nov 2026"

[8AC0001]
market_value = 40

[card_abc]
market_value = 99.99

[card_both]
market_value = 10
"""


def test_entries_are_split_by_cert_and_card_id(tmp_path):
    overrides = load_overrides(write(tmp_path / "o.toml", OVERRIDES_TEXT))
    assert overrides.by_cert == {
        "12345678": Override(market_value=250.0, note="PSA 10 comps, Nov 2026"),
        "8AC0001": Override(market_value=40.0),
    }
    assert overrides.by_card_id == {
        "card_abc": Override(market_value=99.99),
        "card_both": Override(market_value=10.0),
    }


def test_note_is_optional(tmp_path):
    overrides = load_overrides(write(tmp_path / "o.toml", OVERRIDES_TEXT))
    assert overrides.by_cert["8AC0001"].note is None


def test_lookup_by_cert(tmp_path):
    overrides = load_overrides(write(tmp_path / "o.toml", OVERRIDES_TEXT))
    assert overrides.lookup("card_unknown", "12345678") == 250.0


def test_lookup_by_card_id(tmp_path):
    overrides = load_overrides(write(tmp_path / "o.toml", OVERRIDES_TEXT))
    assert overrides.lookup("card_abc", "99999999") == 99.99
    assert overrides.lookup("card_abc", None) == 99.99


def test_lookup_prefers_cert_over_card_id(tmp_path):
    overrides = load_overrides(write(tmp_path / "o.toml", OVERRIDES_TEXT))
    assert overrides.lookup("card_both", "12345678") == 250.0


def test_lookup_miss_returns_none(tmp_path):
    overrides = load_overrides(write(tmp_path / "o.toml", OVERRIDES_TEXT))
    assert overrides.lookup("card_missing", "00000000") is None
    assert overrides.lookup("card_missing", None) is None


def test_zero_market_value_is_allowed(tmp_path):
    overrides = load_overrides(write(tmp_path / "o.toml", "[1]\nmarket_value = 0\n"))
    assert overrides.lookup("card_x", "1") == 0.0


def test_overrides_are_immutable(tmp_path):
    overrides = load_overrides(write(tmp_path / "o.toml", OVERRIDES_TEXT))
    with pytest.raises(TypeError):
        overrides.by_cert["new"] = Override(market_value=1.0)  # type: ignore[index]


def assert_overrides_error(path: Path, key: str) -> None:
    with pytest.raises(ConfigError) as info:
        load_overrides(path)
    message = str(info.value)
    assert key in message
    assert str(path) in message


@pytest.mark.parametrize(
    ("text", "key"),
    [
        ("[1]\nmarket_value = -1\n", "1.market_value"),
        ("[1]\nmarket_value = nan\n", "1.market_value"),
        ("[1]\nmarket_value = inf\n", "1.market_value"),
        ("[1]\nmarket_value = -inf\n", "1.market_value"),
        ('[1]\nmarket_value = "250"\n', "1.market_value"),
        ("[1]\nmarket_value = true\n", "1.market_value"),
        ("[1]\nnote = 'no value'\n", "1.market_value"),
        ("[1]\nmarket_value = 1\nnote = 5\n", "1.note"),
        ("[1]\nmarket_value = 1\nprice = 5\n", "1.price"),
        ("x = 1\n", "x"),
        (f"[1]\nmarket_value = {10**400}\n", "1.market_value"),
        ("[card_]\nmarket_value = 1\n", "card_"),
    ],
)
def test_bad_overrides_raise(tmp_path, text, key):
    assert_overrides_error(write(tmp_path / "o.toml", text), key)


def test_invalid_overrides_toml_raises_config_error(tmp_path):
    path = write(tmp_path / "o.toml", "[1\n")
    with pytest.raises(ConfigError) as info:
        load_overrides(path)
    assert str(path) in str(info.value)


def test_bare_card_prefix_error_says_card_id_is_missing(tmp_path):
    path = write(tmp_path / "o.toml", "[card_]\nmarket_value = 1\n")
    with pytest.raises(ConfigError, match="card id"):
        load_overrides(path)


def test_card_id_keys_are_case_sensitive(tmp_path):
    overrides = load_overrides(write(tmp_path / "o.toml", OVERRIDES_TEXT))
    assert overrides.lookup("CARD_ABC", None) is None
    assert overrides.lookup("card_ABC", None) is None


def test_override_value_is_float(tmp_path):
    overrides = load_overrides(write(tmp_path / "o.toml", "[1]\nmarket_value = 3\n"))
    value = overrides.lookup("card_x", "1")
    assert isinstance(value, float)
    assert math.isclose(value, 3.0)


# --- override_config ---------------------------------------------------------


def test_override_config_replaces_top_level_and_filter_values():
    config = override_config(
        Config(), {"k": 1.2, "filters": {"top": 3, "categories": ["Pokemon"]}}
    )

    assert config.k == 1.2
    assert config.filters.top == 3
    assert config.filters.categories == ("Pokemon",)


def test_override_config_leaves_none_values_and_other_settings_alone(tmp_path):
    base = load_config(
        write(
            tmp_path / "c.toml",
            "k = 0.8\ntax_rate = 0.05\n[filters]\nprice_min = 20\ntop = 4\n"
            '[watch]\ntopic = "my-topic"\n',
        )
    )

    config = override_config(
        base, {"k": None, "filters": {"top": None, "price_max": 90.0}}
    )

    assert config == Config(
        k=0.8,
        tax_rate=0.05,
        filters=Filters(price_min=20.0, price_max=90.0, top=4),
        watch=Watch(topic="my-topic"),
    )


def test_override_config_with_no_changes_round_trips():
    config = Config(
        payment_method=PaymentMethod.ach,
        filters=Filters(categories=("A", "B"), min_ev_pct=-0.1, min_p_profit=0.3),
    )

    assert override_config(config, {}) == config


def test_override_config_does_not_change_its_input():
    base = Config()

    override_config(base, {"k": 2.0})

    assert base == Config()


@pytest.mark.parametrize(
    ("changes", "key"),
    [
        ({"k": 0.0}, "k"),
        ({"k": -1.0}, "k"),
        ({"k": math.nan}, "k"),
        ({"k": math.inf}, "k"),
        ({"filters": {"top": 0}}, "filters.top"),
        ({"filters": {"price_min": -1.0}}, "filters.price_min"),
        ({"filters": {"price_max": -1.0}}, "filters.price_max"),
        ({"filters": {"min_p_profit": 1.5}}, "filters.min_p_profit"),
        ({"filters": {"min_p_profit": -0.1}}, "filters.min_p_profit"),
        ({"filters": {"min_ev_pct": math.nan}}, "filters.min_ev_pct"),
        ({"filters": {"price_min": 200.0, "price_max": 100.0}}, "filters.price_min"),
        ({"filters": {"nonsense": 1}}, "filters.nonsense"),
        ({"bogus": 1}, "bogus"),
    ],
)
def test_override_config_validates_like_the_file(changes, key):
    with pytest.raises(ConfigError) as info:
        override_config(Config(), changes)

    message = str(info.value)
    assert message.startswith("command line: ")
    assert key in message


def test_override_config_checks_price_range_against_the_file_value(tmp_path):
    base = load_config(write(tmp_path / "c.toml", "[filters]\nprice_max = 100\n"))

    with pytest.raises(ConfigError, match="price_min"):
        override_config(base, {"filters": {"price_min": 150.0}})


def test_override_config_names_keys_by_their_display_names():
    names = {"filters.top": "--top", "k": "--k"}

    with pytest.raises(ConfigError) as info:
        override_config(
            Config(), {"filters": {"top": 0}}, source="options", names=names
        )

    assert str(info.value) == "options: --top must be at least 1, got 0"


def test_override_config_display_names_reach_the_price_range_message():
    names = {"filters.price_min": "--price-min", "filters.price_max": "--price-max"}

    with pytest.raises(ConfigError) as info:
        override_config(
            Config(),
            {"filters": {"price_min": 200.0, "price_max": 100.0}},
            names=names,
        )

    assert "--price-min" in str(info.value)
    assert "--price-max" in str(info.value)
    assert "filters." not in str(info.value)


# --- example files ----------------------------------------------------------


def test_example_config_loads():
    config = load_config(REPO_ROOT / "docs" / "config.example.toml")
    assert isinstance(config, Config)


def test_example_overrides_load():
    overrides = load_overrides(REPO_ROOT / "docs" / "overrides.example.toml")
    assert overrides.by_cert
    assert overrides.by_card_id
