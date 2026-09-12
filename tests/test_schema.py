import json
import unittest

import yaml
from pydantic import ValidationError

from cyberpod_core.errors import CoreError
from cyberpod_core.models import LabDefinition
from cyberpod_core.openapi import document
from cyberpod_core.views import lab_view

from .support import ROOT, Harness


class SchemaTests(Harness):
    async def test_invalid_reload_is_atomic(self):
        path = self.path / "labs/hello-lab/lab.yaml"
        path.write_text("id: hello-lab\ninvalid: true\n")
        with self.assertRaises(CoreError):
            self.registry.reload()
        self.assertEqual(self.registry.get("hello-lab").name, "Hello Lab")

    async def test_duplicate_yaml_keys_are_rejected(self):
        path = self.path / "labs/hello-lab/lab.yaml"
        path.write_text(path.read_text() + "\nname: Overwritten\n")
        with self.assertRaises(CoreError):
            self.registry.reload()

    async def test_yaml_aliases_are_rejected(self):
        path = self.path / "labs/hello-lab/lab.yaml"
        path.write_text(path.read_text() + "\nextensions: &anchor {same: *anchor}\n")
        with self.assertRaises(CoreError):
            self.registry.reload()

    async def test_manifest_symlinks_are_rejected(self):
        path = self.path / "labs/hello-lab/lab.yaml"
        saved = self.path / "outside.yaml"
        path.rename(saved)
        path.symlink_to(saved)
        with self.assertRaises(CoreError):
            self.registry.reload()

    async def test_oversized_manifest_is_rejected(self):
        path = self.path / "labs/hello-lab/lab.yaml"
        path.write_text("#" * 262145)
        with self.assertRaises(CoreError):
            self.registry.reload()

    async def test_schema_rejects_unsafe_or_inconsistent_manifests(self):
        cases = {
            "unknown field": lambda d: d.update(hydra_mode=True),
            "global network": lambda d: d["networks"][0].update(scope="global"),
            "hardcoded subnet": lambda d: d["networks"][0].update(subnet="10.0.0.0/24"),
            "privileged": lambda d: d["containers"][0].update(privileged=True),
            "host volume": lambda d: d["containers"][0]["mounts"][0].update(source="/"),
            "invalid network": lambda d: d["containers"][0].update(networks=["unknown"]),
            "invalid volume": lambda d: d["containers"][0]["mounts"][0].update(volume="unknown"),
            "duplicate container": lambda d: d["containers"].append(d["containers"][0].copy()),
            "cyclic tasks": lambda d: d["tasks"][0].update(depends_on=["observe"]),
            "unknown task": lambda d: d["tasks"][0].update(depends_on=["unknown"]),
            "budget exceeded": lambda d: d["resources"].update(memory_mb=64),
            "egress missing": lambda d: d["networks"][0].update(internet="restricted"),
            "irrelevant egress": lambda d: d["networks"][0].update(egress_allowlist=["example.test"]),
            "browser missing endpoint": lambda d: d["browser"].update(required=True),
            "unknown browser port": lambda d: d["browser"].update(required=True, container="greeting", port=6080),
            "flag without validator": lambda d: d.update(flags=[{"id":"completion","label":"Flag"}]),
        }
        for name, mutate in cases.items():
            with self.subTest(name=name):
                definition = yaml.safe_load((ROOT / "labs/hello-lab/lab.yaml").read_text())
                mutate(definition)
                with self.assertRaises(ValidationError):
                    LabDefinition.model_validate(definition)

    async def test_extensions_are_explicit_and_private_by_default(self):
        self.edit_manifest(lambda d: d.update(extensions={"org.example.notes": {"secret": "PRIVATE"}}))
        definition = self.registry.get("hello-lab")
        self.assertIn("org.example.notes", definition.extensions)
        public = lab_view(definition, self.engine).model_dump_json()
        self.assertNotIn("PRIVATE", public)
        self.assertIn("UNAVAILABLE", public)

    async def test_snapshot_is_independent_of_registry_object_mutation(self):
        lab = self.registry.get("hello-lab")
        lab.containers[0].environment["secret"] = "changed"
        self.assertEqual(self.registry.get("hello-lab").containers[0].environment, {})


class ContractTests(unittest.TestCase):
    def test_openapi_component_references_resolve(self):
        contract = document()
        def visit(value):
            if isinstance(value, dict):
                if "$ref" in value:
                    target = contract
                    for part in value["$ref"].removeprefix("#/").split("/"):
                        target = target[part]
                for child in value.values():
                    visit(child)
            elif isinstance(value, list):
                for child in value:
                    visit(child)
        visit(contract)
        self.assertEqual(contract["openapi"], "3.1.0")

    def test_exported_openapi_matches_runtime_contract(self):
        self.assertEqual(json.loads((ROOT / "schemas/openapi.json").read_text()), document())

    def test_exported_lab_schema_matches_models(self):
        exported = json.loads((ROOT / "schemas/lab.schema.json").read_text())
        exported.pop("$schema")
        self.assertEqual(exported, LabDefinition.model_json_schema())

