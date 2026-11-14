# SPDX-License-Identifier: AGPL-3.0-or-later
import pytest

from bambu_lan_cli.config import init_config, load_config, validate, validate_via
from bambu_lan_cli.errors import CliError

TEXT = 'host: 192.168.1.100\nserial: 01P00A000000000\naccess_code: "00000000"\n'


def save(path, text=TEXT, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(mode)
    return path


def test_priority_and_fallback(tmp_path):
    cwd = tmp_path / "work"
    cwd.mkdir()
    home = tmp_path / "home"
    save(
        home / ".config/bambu/p2s.env",
        'export BAMBU_HOST=192.168.1.100\nBAMBU_SERIAL=01P00A000000000\nBAMBU_ACCESS_CODE="00000000"\n',
    )
    assert load_config(cwd=cwd, home=home).model == "p2s"
    save(home / ".config/bambu/config.yaml", TEXT + "model: home\n")
    assert load_config(cwd=cwd, home=home).model == "home"
    save(cwd / "config.yaml", TEXT + "model: cwd\n")
    assert load_config(cwd=cwd, home=home).model == "cwd"
    save(cwd / "config.local.yaml", TEXT + "model: local\n")
    assert load_config(cwd=cwd, home=home).model == "local"
    explicit = save(tmp_path / "explicit.yaml", TEXT + "model: explicit\n")
    assert load_config(explicit, cwd=cwd, home=home).model == "explicit"
    with pytest.raises(CliError):
        load_config(tmp_path / "missing.yaml", cwd=cwd, home=home)


@pytest.mark.parametrize("mode", [0o644, 0o400, 0o660, 0o777])
def test_permissions(tmp_path, mode):
    with pytest.raises(CliError):
        load_config(save(tmp_path / "secret.yaml", mode=mode))


def test_symlink(tmp_path):
    real = save(tmp_path / "real.yaml")
    link = tmp_path / "link.yaml"
    link.symlink_to(real)
    with pytest.raises(CliError):
        load_config(link)


def test_yaml_error_does_not_echo(tmp_path):
    with pytest.raises(CliError) as e:
        load_config(save(tmp_path / "bad.yaml", "access_code: [private-marker"))
    assert "private-marker" not in str(e.value)


@pytest.mark.parametrize(
    "host", ["8.8.8.8", "127.0.0.1", "169.254.1.1", "100.64.0.1", "::1", "224.0.0.1"]
)
def test_rfc1918(host):
    with pytest.raises(CliError):
        validate(dict(host=host, serial="01P00A000000000", access_code="00000000"))


@pytest.mark.parametrize(
    "patch",
    [
        {"serial": "bad/path"},
        {"access_code": ""},
        {"access_code": 0},
        {"model": "../bad"},
    ],
)
def test_validation(patch):
    with pytest.raises(CliError):
        validate(
            dict(
                dict(
                    host="192.168.1.100",
                    serial="01P00A000000000",
                    access_code="00000000",
                ),
                **patch,
            )
        )


def test_init(tmp_path):
    path = tmp_path / "new/config.yaml"
    init_config(path)
    assert path.stat().st_mode & 0o777 == 0o600
    before = path.read_bytes()
    init_config(path)
    assert path.read_bytes() == before


@pytest.mark.parametrize("via", ["-oProxyCommand=x", "host;bad", "host bad", "../host"])
def test_via(via):
    with pytest.raises(CliError):
        validate_via(via)
