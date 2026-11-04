"""Run in an isolated agent container; Docker and scanner commands are stubs."""

import csv
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "run-srpm-scan"

DOCKER_STUB = """#!/usr/bin/env python3
import json
import os
from pathlib import Path
import subprocess
import sys

args = sys.argv[1:]
if args[:2] == ["rm", "-f"]:
    raise SystemExit(0)
if args[:2] != ["run", "--rm"]:
    raise SystemExit("unexpected Docker invocation")
Path(os.environ["TEST_DOCKER_ARGS"]).write_text(json.dumps(args))
image_index = args.index(os.environ["CSMOCK_IMAGE"])
# Exercise the actual inner shell, but never start a Docker container.
raise SystemExit(subprocess.run(args[image_index + 1:]).returncode)
"""

CSMOCK_STUB = """#!/usr/bin/env python3
import json
import os
from pathlib import Path
import sys

Path(os.environ["TEST_CSMOCK_ARGS"]).write_text(json.dumps(sys.argv[1:]))
raise SystemExit(int(os.environ.get("TEST_SCANNER_EXIT", "0")))
"""

SUDO_STUB = """#!/usr/bin/env python3
import json
import os
from pathlib import Path
import sys

Path(os.environ["TEST_SUDO_ARGS"]).write_text(json.dumps(sys.argv[1:]))
raise SystemExit(int(os.environ.get("TEST_SUDO_EXIT", "0")))
"""


class RunSrpmScanTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="srpm-argv-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.workdir = self.root / "agent" / "workspace" / "scan job"
        self.workdir.mkdir(parents=True)
        self.fakebin = self.root / "bin"
        self.fakebin.mkdir()
        for name, code in (("docker", DOCKER_STUB), ("csmock", CSMOCK_STUB), ("sudo", SUDO_STUB)):
            executable = self.fakebin / name
            executable.write_text(code)
            executable.chmod(0o755)
        self.env = {
            **os.environ,
            "PATH": f"{self.fakebin}:{os.environ['PATH']}",
            "JENKINS_AGENT_WORKDIR": str(self.root / "agent"),
            "WORKSPACE": str(self.workdir),
            "SRPM_SCAN_INPUT_HOST_PATH": "/host/input with spaces",
            "SRPM_SCAN_AGENT_WORKSPACE_HOST_PATH": "/host/agent workspace",
            "CSMOCK_IMAGE": "test-csmock:fixed",
            "MOCK_CONFIG": "loongnix-server-23-loongarch64",
            "CSMOCK_TOOLS": "cppcheck,clang,gcc,shellcheck",
            "BUILD_NUMBER": "7",
            "TEST_DOCKER_ARGS": str(self.root / "docker.json"),
            "TEST_CSMOCK_ARGS": str(self.root / "csmock.json"),
            "TEST_SUDO_ARGS": str(self.root / "sudo.json"),
        }

    def invoke(self, name="curl-8.4.0-9.ocs23.src.rpm", output="artifacts", *, dry_run=False, input_exists=True, **env):
        if input_exists and "/" not in name and name.endswith(".src.rpm"):
            source = Path("/scan-input") / name
            source.write_bytes(b"test RPM input")
            self.addCleanup(source.unlink, missing_ok=True)
        args = ["bash", str(SCRIPT)]
        if dry_run:
            args.append("--dry-run")
        return subprocess.run(
            [*args, name, output], cwd=self.workdir, env={**self.env, **env},
            capture_output=True, text=True, timeout=10,
        )

    def read_args(self, command):
        return json.loads((self.root / f"{command}.json").read_text())

    def bind_mounts(self):
        args = self.read_args("docker")
        self.assertNotIn("-v", args, "bind sources must not be created silently by Docker")
        result = []
        for index, arg in enumerate(args):
            if arg == "--mount":
                fields = next(csv.reader([args[index + 1]]))
                result.append(dict(field.split("=", 1) if "=" in field else (field, True) for field in fields))
        self.assertEqual(len(result), 2)
        return result

    def assert_no_injection(self):
        self.assertFalse((self.workdir / "PROBE").exists(), "untrusted text ran as shell code")

    def assert_scan_args(self, name, output="artifacts", config=None, tools="cppcheck,clang,gcc,shellcheck"):
        expected = ["-r", config or self.env["MOCK_CONFIG"]]
        if tools:
            expected += ["-t", tools]
        expected += ["--force", "-o", f"/output/{name[:-8]}.tar.xz", f"/input/{name}"]
        self.assertEqual(self.read_args("csmock"), expected)

    def test_normal_scan_preserves_arguments_and_ownership(self):
        name = "curl-8.4.0-9.ocs23.src.rpm"
        result = self.invoke(name)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_scan_args(name)
        self.assertEqual(self.read_args("sudo"), [
            "chown", "-R", f"{os.getuid()}:{os.getgid()}", "--", "/output",
        ])

    def test_filename_shell_metacharacters_remain_literal(self):
        for name in (
            "curl with spaces.src.rpm", "curl'; touch PROBE; #.src.rpm",
            'curl"$(touch PROBE)".src.rpm', "--flag.src.rpm",
        ):
            with self.subTest(name=name):
                result = self.invoke(name)
                self.assert_no_injection()
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assert_scan_args(name)

    def test_mock_config_is_one_literal_argument(self):
        config = "profile'; touch PROBE; #"
        result = self.invoke(MOCK_CONFIG=config)
        self.assert_no_injection()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_scan_args("curl-8.4.0-9.ocs23.src.rpm", config=config)

    def test_output_path_is_not_shell_code(self):
        output = "artifacts'; touch PROBE; #"
        result = self.invoke(output=output)
        self.assert_no_injection()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_scan_args("curl-8.4.0-9.ocs23.src.rpm", output=output)

    def test_output_directory_starting_with_dash_is_data(self):
        result = self.invoke(output="--version")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_scan_args("curl-8.4.0-9.ocs23.src.rpm", output="--version")

    def test_empty_tools_keeps_csmock_default_selection(self):
        result = self.invoke(CSMOCK_TOOLS="")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_scan_args("curl-8.4.0-9.ocs23.src.rpm", tools="")

    def test_invalid_tool_lists_are_rejected_before_docker(self):
        for tools in ("clang; touch PROBE", "gcc$(touch PROBE)", "--help", "clang cppcheck", "gcc,,clang", "gcc,", "gcc\nclang"):
            with self.subTest(tools=tools):
                result = self.invoke(CSMOCK_TOOLS=tools)
                self.assert_no_injection()
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertFalse((self.root / "docker.json").exists())

    def test_plugin_names_are_not_hardcoded(self):
        tools = "custom_checker,another-checker,plugin.v2"
        result = self.invoke(CSMOCK_TOOLS=tools)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_scan_args("curl-8.4.0-9.ocs23.src.rpm", tools=tools)

    def test_invalid_mock_config_is_rejected_before_docker(self):
        for config in ("--help", "profile\nother", "profile\tother"):
            with self.subTest(config=config):
                result = self.invoke(MOCK_CONFIG=config)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertFalse((self.root / "docker.json").exists())

    def test_image_cannot_inject_docker_options(self):
        for image in ("--privileged", "--network=host", "image\nother"):
            with self.subTest(image=image):
                result = self.invoke(CSMOCK_IMAGE=image)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertFalse((self.root / "docker.json").exists())

    def test_invalid_filenames_are_rejected_before_docker(self):
        for name in ("../curl.src.rpm", "sub/curl.src.rpm", "sub\\curl.src.rpm", "curl\n.src.rpm", "curl\t.src.rpm", "curl.rpm"):
            with self.subTest(name=name):
                result = self.invoke(name)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertFalse((self.root / "docker.json").exists())

    def test_scanner_failure_keeps_exit_code_and_skips_chown(self):
        result = self.invoke(TEST_SCANNER_EXIT="17")
        self.assertEqual(result.returncode, 17, result.stderr)
        self.assertFalse((self.root / "sudo.json").exists())

    def test_ownership_failure_is_not_reported_as_success(self):
        result = self.invoke(TEST_SUDO_EXIT="23")
        self.assertEqual(result.returncode, 23, result.stderr)
        self.assertNotIn("SRPM 扫描产物：", result.stdout)

    def test_missing_input_is_rejected_before_docker(self):
        result = self.invoke(input_exists=False)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertFalse((self.root / "docker.json").exists())

    def test_output_outside_agent_workspace_is_rejected(self):
        result = self.invoke(output=str(self.root / "outside"))
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertFalse((self.root / "docker.json").exists())

    def test_dry_run_does_not_require_an_existing_input(self):
        result = self.invoke(dry_run=True, input_exists=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.root / "docker.json").exists())
        self.assertFalse((self.workdir / "artifacts").exists())

    def test_dry_run_has_no_side_effects_and_round_trips_arguments(self):
        name = "curl'; touch PROBE; #.src.rpm"
        result = self.invoke(name, dry_run=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.root / "docker.json").exists())
        self.assertFalse((self.workdir / "artifacts").exists())
        replay = subprocess.run(
            ["bash", "-c", result.stdout], cwd=self.workdir, env=self.env,
            capture_output=True, text=True, timeout=10,
        )
        self.assert_no_injection()
        self.assertEqual(replay.returncode, 0, replay.stderr)
        self.assert_scan_args(name)

    def test_only_current_input_and_output_are_bound(self):
        name = "curl-8.4.0-9.ocs23.src.rpm"
        result = self.invoke(name)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.bind_mounts(), [
            {"type": "bind", "source": f"/host/input with spaces/{name}",
             "target": f"/input/{name}", "readonly": True},
            {"type": "bind", "source": "/host/agent workspace/workspace/scan job/artifacts",
             "target": "/output"},
        ])
        self.assertIn("/host/agent workspace/workspace/scan job/artifacts/", result.stdout)

    def test_mount_paths_preserve_commas_quotes_and_colons(self):
        name = 'curl,"quoted":next.src.rpm'
        host_input = '/host/input,"quoted":data'
        host_workspace = '/host/agent,"quoted":data'
        result = self.invoke(name, output='output,"quoted":data',
                             SRPM_SCAN_INPUT_HOST_PATH=host_input,
                             SRPM_SCAN_AGENT_WORKSPACE_HOST_PATH=host_workspace)
        self.assertEqual(result.returncode, 0, result.stderr)
        mounts = self.bind_mounts()
        self.assertEqual(mounts[0]["source"], f"{host_input}/{name}")
        self.assertEqual(mounts[0]["target"], f"/input/{name}")
        self.assertEqual(mounts[1]["source"], f'{host_workspace}/workspace/scan job/output,"quoted":data')

    def test_commas_in_paths_cannot_add_mount_options(self):
        name = "curl,readonly=false.src.rpm"
        result = self.invoke(name, output="output,target=elsewhere",
                             SRPM_SCAN_INPUT_HOST_PATH="/host/input,readonly=false,target=/etc")
        self.assertEqual(result.returncode, 0, result.stderr)
        mounts = self.bind_mounts()
        self.assertEqual(set(mounts[0]), {"type", "source", "target", "readonly"})
        self.assertIs(mounts[0]["readonly"], True)
        self.assertEqual(mounts[0]["target"], f"/input/{name}")
        self.assertEqual(mounts[1]["target"], "/output")

    def test_unset_workspace_falls_back_to_current_directory(self):
        result = self.invoke(WORKSPACE="")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.bind_mounts()[1]["source"],
                         "/host/agent workspace/workspace/scan job/artifacts")

    def test_workspace_cannot_escape_agent_root(self):
        result = self.invoke(WORKSPACE=str(self.root / "outside"))
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertFalse((self.root / "docker.json").exists())

    def test_control_characters_in_output_path_are_rejected(self):
        result = self.invoke(output="artifacts\nother")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertFalse((self.root / "docker.json").exists())

    def test_output_cannot_be_another_jobs_directory(self):
        outside = self.root / "agent" / "workspace" / "other job" / "artifacts"
        result = self.invoke(output=str(outside))
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertFalse((self.root / "docker.json").exists())
        self.assertFalse(outside.exists())

    def test_entire_current_workspace_cannot_be_the_output(self):
        result = self.invoke(output=".")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertFalse((self.root / "docker.json").exists())

    def test_output_symlink_cannot_escape_current_workspace(self):
        outside = self.root / "agent" / "workspace" / "other job"
        outside.mkdir()
        (self.workdir / "link").symlink_to(outside, target_is_directory=True)
        result = self.invoke(output="link/artifacts")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertFalse((self.root / "docker.json").exists())
        self.assertFalse((outside / "artifacts").exists())

    def test_input_symlink_is_rejected(self):
        outside = self.root / "outside.src.rpm"
        outside.write_bytes(b"unrelated file")
        link = Path("/scan-input/curl-link.src.rpm")
        link.symlink_to(outside)
        self.addCleanup(link.unlink, missing_ok=True)
        result = self.invoke(link.name, input_exists=False)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertFalse((self.root / "docker.json").exists())

    def test_input_must_be_a_regular_file(self):
        directory = Path("/scan-input/not-a-file.src.rpm")
        directory.mkdir()
        self.addCleanup(directory.rmdir)
        result = self.invoke(directory.name, input_exists=False)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertFalse((self.root / "docker.json").exists())

    def test_invalid_host_roots_are_rejected(self):
        for field in ("SRPM_SCAN_INPUT_HOST_PATH", "SRPM_SCAN_AGENT_WORKSPACE_HOST_PATH"):
            for value in ("relative", "/", "///", "/host/../data", "/host\ndata"):
                with self.subTest(field=field, value=value):
                    result = self.invoke(**{field: value})
                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertFalse((self.root / "docker.json").exists())

    def test_independent_outputs_have_independent_writable_mounts(self):
        first = self.invoke(output="artifacts/first")
        self.assertEqual(first.returncode, 0, first.stderr)
        first_source = self.bind_mounts()[1]["source"]
        second = self.invoke(output="artifacts/second")
        self.assertEqual(second.returncode, 0, second.stderr)
        second_source = self.bind_mounts()[1]["source"]
        self.assertEqual(first_source, "/host/agent workspace/workspace/scan job/artifacts/first")
        self.assertEqual(second_source, "/host/agent workspace/workspace/scan job/artifacts/second")
        self.assertNotEqual(first_source, second_source)


if __name__ == "__main__":
    unittest.main()
