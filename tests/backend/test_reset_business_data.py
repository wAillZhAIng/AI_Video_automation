import importlib.util
from pathlib import Path
import sqlite3
import tempfile
import unittest


spec = importlib.util.spec_from_file_location(
    "reset_business_data", Path(__file__).resolve().parents[2] / "scripts/reset_business_data.py"
)
reset_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reset_module)


class BusinessResetTest(unittest.TestCase):
    def test_keeps_model_configuration_and_removes_business_data(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            media = root / "data/media"
            media.mkdir(parents=True)
            key = root / "data/.secrets/master.key"
            key.parent.mkdir()
            key.write_bytes(b"disposable test key")
            workflow = media / "models/registered-workflow.json"
            workflow.parent.mkdir()
            workflow.write_text("{}", encoding="utf-8")
            upload = media / "uploaded-picture.png"
            upload.write_bytes(b"disposable upload")
            receipt = root / "reports/model-check.json"
            receipt.parent.mkdir()
            receipt.write_text("{}", encoding="utf-8")
            (receipt.parent / "run.log").touch()
            database = root / "data/video_factory.db"
            with sqlite3.connect(database) as connection:
                connection.executescript("""
                    CREATE TABLE projects (id TEXT PRIMARY KEY);
                    CREATE TABLE jobs (project TEXT REFERENCES projects(id));
                    CREATE TABLE ai_services (id TEXT PRIMARY KEY, encrypted_secret TEXT, settings_json TEXT);
                    CREATE TABLE local_models (service_id TEXT REFERENCES ai_services(id), workflow_path TEXT);
                    CREATE TABLE quality_profiles (scope TEXT);
                    CREATE TABLE route_policies (scope TEXT);
                    INSERT INTO projects VALUES ('business');
                    INSERT INTO jobs VALUES ('business');
                    INSERT INTO ai_services VALUES ('persist', 'ciphertext', '{}');
                    INSERT INTO local_models VALUES ('persist', 'models/registered-workflow.json');
                    INSERT INTO quality_profiles VALUES ('system'), ('project');
                    INSERT INTO route_policies VALUES ('system'), ('shot');
                """)
            connection.close()

            self.assertEqual(reset_module.reset(root, retained_files=(receipt,)), 4)
            self.assertEqual(reset_module.reset(root, retained_files=(receipt,)), 0)
            with sqlite3.connect(database) as connection:
                self.assertEqual(connection.execute("SELECT * FROM jobs").fetchall(), [])
                self.assertEqual(connection.execute("SELECT * FROM projects").fetchall(), [])
                self.assertEqual(connection.execute("SELECT encrypted_secret FROM ai_services").fetchone(), ("ciphertext",))
                self.assertEqual(connection.execute("SELECT service_id FROM local_models").fetchone(), ("persist",))
                self.assertEqual(connection.execute("SELECT scope FROM quality_profiles").fetchall(), [("system",)])
                self.assertEqual(connection.execute("SELECT scope FROM route_policies").fetchall(), [("system",)])
            connection.close()
            self.assertFalse(upload.exists())
            self.assertFalse((receipt.parent / "run.log").exists())
            self.assertEqual(workflow.read_text(encoding="utf-8"), "{}")
            self.assertTrue(receipt.is_file())
            self.assertEqual(key.read_bytes(), b"disposable test key")

    def test_rejects_an_unrelated_database_before_removing_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            media = root / "data/media"
            media.mkdir(parents=True)
            upload = media / "import"
            upload.touch()
            database = root / "data/video_factory.db"
            with sqlite3.connect(database) as connection:
                connection.execute("CREATE TABLE unrelated (id TEXT)")
            connection.close()
            with self.assertRaisesRegex(ValueError, "not an initialized Video Factory database"):
                reset_module.reset(root)
            self.assertTrue(upload.is_file())
