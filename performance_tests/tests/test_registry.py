from django.test import SimpleTestCase

from performance_tests.registry import AIS_DETECTORS, FUSION_DETECTORS


class RegistryTests(SimpleTestCase):
    def test_registers_exactly_sixteen_integrated_models(self):
        self.assertEqual(len(AIS_DETECTORS), 14)
        self.assertEqual(
            FUSION_DETECTORS,
            frozenset({"detect-ais-off", "detect-spoofing"}),
        )

