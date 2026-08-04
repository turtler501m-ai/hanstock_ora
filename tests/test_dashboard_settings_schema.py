import unittest

from src.dashboard.settings_schema import ENV_FIELDS, ENV_FIELD_MAP


class DashboardSettingsSchemaTests(unittest.TestCase):
    def test_keys_are_unique_and_map_is_complete(self):
        keys = [field["key"] for field in ENV_FIELDS]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertEqual(set(keys), set(ENV_FIELD_MAP))

    def test_every_field_has_complete_display_and_runtime_metadata(self):
        required = {
            "key",
            "type",
            "label",
            "hint",
            "options",
            "secret",
            "restart_required",
            "runtime_binding",
        }
        for field in ENV_FIELDS:
            with self.subTest(key=field["key"]):
                self.assertTrue(required <= set(field))
                self.assertIsInstance(field["options"], list)
                self.assertEqual(field["secret"], field["type"] == "secret")

    def test_select_fields_define_options(self):
        for field in ENV_FIELDS:
            if field["type"] == "select":
                with self.subTest(key=field["key"]):
                    self.assertTrue(field["options"])


if __name__ == "__main__":
    unittest.main()
