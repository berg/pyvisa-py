"""Tests for the VXI-11 protocol layer.

:copyright: 2014-2024 by PyVISA-py Authors, see AUTHORS for more details.
:license: MIT, see LICENSE for more details.

"""

import pytest

from pyvisa.constants import StatusCode
from pyvisa_py.protocols import vxi11
from pyvisa_py.tcpip import VXI11_ERRORS_TO_VISA, vxi11_error_to_status


@pytest.mark.parametrize("error", sorted(VXI11_ERRORS_TO_VISA))
def test_defined_error_codes_keep_their_mapping(error):
    assert vxi11_error_to_status(error) is VXI11_ERRORS_TO_VISA[error]


@pytest.mark.parametrize("error", [2, 7, 21, 100, 200, 255])
def test_every_error_code_maps_to_a_status(error):
    """A code outside the table must not raise out of the VISA call.

    VXI-11 B.5.2 Table B.2 defines the codes in the table. Servers do return
    others, and the lookup used to be a subscript, so viReadSTB against such a
    server raised KeyError instead of reporting an error.
    """
    assert isinstance(vxi11_error_to_status(error), StatusCode)


def test_invalid_address_is_mapped():
    """VXI-11 B.5.2 Table B.2 defines error 21 as invalid address.

    Table B.4 lists it only for create_link, so it reports a device the
    server cannot address rather than a failure of an established link.
    """
    assert vxi11.ErrorCodes.invalid_address == 21
    assert vxi11_error_to_status(21) is StatusCode.error_resource_not_found


def test_error_codes_enum_covers_the_status_table():
    assert {int(code) for code in vxi11.ErrorCodes} == set(VXI11_ERRORS_TO_VISA)
