from src import homewizard_api_client as hwc
from src.meter_device import MeterDevice


class TestMeterDeviceProtocol:
    def test_homewizard_device_satisfies_the_protocol_structurally(self):
        # runtime_checkable Protocol -- this only checks method/attribute
        # presence, not signatures, but it's the contract ingest_all() relies
        # on: anything with .name/.is_configured()/.fetch_measurement() can
        # be polled generically.
        assert isinstance(hwc.HomeWizardDevice("p1"), MeterDevice)
