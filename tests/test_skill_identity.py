import importlib.util
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location("validate_skills", Path(__file__).parents[1] / "scripts/validate_skills.py")
validator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(validator)


class IdentityValidationTests(unittest.TestCase):
    def test_duplicate_id_is_rejected_even_after_name_and_directory_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            for name in ("图片生成", "图片编辑"):
                skill = Path(directory) / name
                skill.mkdir()
                (skill / "SKILL.md").write_text(f"---\nname: {name}\ndescription: 测试技能\nmetadata:\n  skill_id: 11111111-1111-4111-8111-111111111111\n  version: \"1.0.0\"\n---\n", encoding="utf-8")
            self.assertTrue(any("重复" in error for error in validator.validate_repository(Path(directory))))

    def test_missing_and_invalid_ids_fail_publication_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            skill = Path(directory) / "图片生成"
            skill.mkdir()
            for metadata in ("", "metadata:\n  skill_id: not-a-uuid\n"):
                (skill / "SKILL.md").write_text("---\nname: 图片生成\ndescription: 测试技能\n" + metadata + "---\n", encoding="utf-8")
                self.assertTrue(validator.validate_repository(Path(directory)))

    def test_missing_invalid_and_non_string_versions_fail_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            skill = Path(directory) / "图片生成"
            skill.mkdir()
            for version in (None, "1.0", "v1.0.0", "01.0.0", "1.0.-1", 1):
                with self.subTest(version=version):
                    declaration = "" if version is None else "  version: " + (str(version) if type(version) is int else f'"{version}"') + "\n"
                    (skill / "SKILL.md").write_text("---\nname: 图片生成\ndescription: 测试技能\nmetadata:\n  skill_id: 11111111-1111-4111-8111-111111111111\n" + declaration + "---\n", encoding="utf-8")
                    self.assertTrue(any("metadata.version" in error for error in validator.validate_repository(Path(directory))))

    def test_release_versions_keep_the_same_skill_identity_valid(self):
        with tempfile.TemporaryDirectory() as directory:
            skill = Path(directory) / "图片生成"
            skill.mkdir()
            for version in ("1.0.0", "1.0.1", "1.1.0", "1.10.0", "2.0.0"):
                with self.subTest(version=version):
                    (skill / "SKILL.md").write_text("---\nname: 图片生成\ndescription: 测试技能\nmetadata:\n  skill_id: 11111111-1111-4111-8111-111111111111\n  version: \"" + version + "\"\n---\n", encoding="utf-8")
                    self.assertEqual(validator.validate_repository(Path(directory)), [])


if __name__ == "__main__":
    unittest.main()
