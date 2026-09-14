"""Tests for runtime_secrets.hydrate.

No AWS calls: the SSM client is faked. What is worth testing here is not boto3 —
it is the three behaviours the deployed runtime depends on:

  1. With no LEADGEN_SECRETS_PATH it does nothing, so local dev is untouched.
  2. It strips the path prefix, because the runtime wants SUPABASE_URL, not
     /leadgen/SUPABASE_URL.
  3. It REFUSES TO BOOT when the path is set but SSM returns nothing. That is the
     whole point of the module: a runtime that starts with no credentials and
     discovers it halfway through a paid run is much worse than one that fails
     loudly at startup.

It also covers pagination, because get_parameters_by_path returns at most 10
parameters per call by default and this project has 15.
"""

import pytest

from runtime_secrets import SECRETS_PATH_ENV, hydrate


class FakePaginator:
    def __init__(self, pages):
        self._pages = pages
        self.calls = []

    def paginate(self, **kwargs):
        self.calls.append(kwargs)
        return iter(self._pages)


class FakeSSM:
    """Stands in for a boto3 SSM client. Records what it was asked for."""

    def __init__(self, pages):
        self.paginator = FakePaginator(pages)

    def get_paginator(self, name):
        assert name == "get_parameters_by_path"
        return self.paginator


def _param(name, value):
    return {"Name": name, "Value": value, "Type": "SecureString"}


def test_no_path_configured_is_a_no_op():
    env = {}
    report = hydrate(client=FakeSSM([]), env=env)
    assert report["loaded"] == 0
    assert "skipped" in report
    assert env == {}


def test_a_locally_loaded_env_var_beats_ssm():
    """Local dev exports the real value; SSM must not clobber it."""
    env = {"SUPABASE_URL": "http://localhost:54321"}
    hydrate(path="/leadgen/", client=FakeSSM([{"Parameters": [
        _param("/leadgen/SUPABASE_URL", "https://prod.example.co")]}]), env=env)
    assert env["SUPABASE_URL"] == "http://localhost:54321"


def test_values_are_unprefixed_into_the_environment():
    env = {}
    report = hydrate(path="/leadgen/", client=FakeSSM([{"Parameters": [
        _param("/leadgen/GROQ_API_KEY", "gsk_x"),
        _param("/leadgen/SUPABASE_URL", "https://x.supabase.co"),
    ]}]), env=env)

    assert env["GROQ_API_KEY"] == "gsk_x"
    assert env["SUPABASE_URL"] == "https://x.supabase.co"
    assert report["loaded"] == 2
    assert report["names"] == ["GROQ_API_KEY", "SUPABASE_URL"]


def test_the_report_carries_names_but_never_values():
    """This report gets logged at startup, so it must be safe to log."""
    report = hydrate(path="/leadgen/", client=FakeSSM([{"Parameters": [
        _param("/leadgen/DATAFORSEO_PASSWORD", "hunter2")]}]), env={})
    assert "hunter2" not in repr(report)


def test_it_paginates_past_the_ten_parameter_default():
    pages = [{"Parameters": [_param(f"/leadgen/VAR_{i}", str(i)) for i in range(10)]},
             {"Parameters": [_param(f"/leadgen/VAR_{i}", str(i)) for i in range(10, 15)]}]
    env = {}
    report = hydrate(path="/leadgen/", client=FakeSSM(pages), env=env)

    assert report["loaded"] == 15
    assert env["VAR_14"] == "14"


def test_the_fetch_is_encrypted_and_recursive():
    """SecureString needs WithDecryption; Recursive is what lets the path hold
    the whole config flat under one prefix."""
    client = FakeSSM([{"Parameters": [_param("/leadgen/X", "1")]}])
    hydrate(path="/leadgen/", client=client, env={})

    assert client.paginator.calls == [
        {"Path": "/leadgen/", "WithDecryption": True, "Recursive": True}]


def test_an_empty_prefix_refuses_to_boot():
    """The failure this module exists to prevent: path configured, zero values
    loaded, runtime starts anyway and dies mid-run with empty credentials."""
    with pytest.raises(RuntimeError) as exc:
        hydrate(path="/leadgen/", client=FakeSSM([]), env={})

    assert SECRETS_PATH_ENV in str(exc.value)
    assert "ssm:GetParametersByPath" in str(exc.value)


def test_the_path_comes_from_the_environment_by_default():
    client = FakeSSM([{"Parameters": [_param("/leadgen/TAGGED", "yes")]}])
    env = {SECRETS_PATH_ENV: "/leadgen/"}
    hydrate(client=client, env=env)

    assert client.paginator.calls[0]["Path"] == "/leadgen/"
    assert env["TAGGED"] == "yes"
