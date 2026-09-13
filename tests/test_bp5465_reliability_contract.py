"""Regression contract for BP5465 poll reliability semantics."""

from pathlib import Path
import re


REPO = Path(__file__).resolve().parents[1]
INIT = REPO / "custom_components" / "omron" / "__init__.py"
PARSER = REPO / "custom_components" / "omron" / "omron_ble" / "parser.py"


def test_forced_transfer_is_latched_while_ble_lock_is_held():
    source = INIT.read_text(encoding="utf-8")

    assert 'entry_data["pending_forced_transfer"] = True' in source
    assert '"pending_forced_transfer_task"' in source
    assert '"pending_forced_transfer_baseline"' in source
    assert '"force_poll_after_lock"' in source
    assert "latched forced-transfer trigger" in source
    assert "Draining latched forced-transfer trigger" in source


def test_explicit_forced_transfer_can_wait_behind_one_active_session():
    source = INIT.read_text(encoding="utf-8")

    assert "Forced-transfer poll waiting for active BLE session " in source
    assert "to release lock for %s" in source


def test_poll_coordinator_errors_are_not_converted_to_cached_success():
    source = INIT.read_text(encoding="utf-8")

    timeout_block = re.search(
        r"except TimeoutError:(.*?)(?=\n        except Exception)",
        source,
        re.S,
    )
    assert timeout_block is not None
    assert "raise" in timeout_block.group(1)
    assert "return poll_coordinator.data" not in timeout_block.group(1)

    generic_block = re.search(
        r'except Exception as err:\n'
        r'\s+# Preserve the previous coordinator data(.*?)(?=\n        finally:)',
        source,
        re.S,
    )
    assert generic_block is not None
    assert "raise" in generic_block.group(1)
    assert "return poll_coordinator.data" not in generic_block.group(1)


def test_parser_propagates_real_poll_failures():
    source = PARSER.read_text(encoding="utf-8")

    connection_block = re.search(
        r"except ConnectionError as exc:(.*?)(?=\n            except Exception as exc:)",
        source,
        re.S,
    )
    assert connection_block is not None
    assert "raise" in connection_block.group(1)

    generic_block = re.search(
        r"except Exception as exc:(.*?)\n\n            return self\._finish_update\(\)",
        source,
        re.S,
    )
    assert generic_block is not None
    assert "Poll failed model=" in generic_block.group(1)
    assert "raise" in generic_block.group(1)


def test_required_service_and_fallback_readout_fail_closed():
    source = PARSER.read_text(encoding="utf-8")

    assert (
        "raise ConnectionError("
        in source
    )

    fallback = re.search(
        r"except Exception as fallback_exc:(.*?)(?=\n                    else:)",
        source,
        re.S,
    )
    assert fallback is not None
    assert "Fallback memory session readout failed" in fallback.group(1)
    assert "raise" in fallback.group(1)



def test_forced_transfer_marker_is_consumed_before_device_lookup():
    source = INIT.read_text(encoding="utf-8")

    poll_start = source.index("    async def _async_poll_data(")
    poll_end = source.index(
        "\n    scan_interval = entry.options.get(",
        poll_start,
    )
    poll_source = source[poll_start:poll_end]

    marker_pos = poll_source.index(
        'force_poll_after_lock = bool('
    )
    device_lookup_pos = poll_source.index(
        "device = async_ble_device_from_address(hass, address)"
    )

    assert marker_pos < device_lookup_pos

    no_device_start = poll_source.index("if not device:")
    coordinator_start = poll_source.index(
        "coordinator = entry.runtime_data",
        no_device_start,
    )
    no_device_block = poll_source[
        no_device_start:coordinator_start
    ]

    assert "if force_poll_after_lock:" in no_device_block
    assert "raise ConnectionError(" in no_device_block
    assert "disappeared before forced-transfer poll" in no_device_block
