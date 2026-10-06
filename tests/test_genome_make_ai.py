import sys
import random
import re
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ai_lab"))
import genome as G
import make_ai as MK


def builder_template():
    return "\n".join(
        "(defconst %s 0)" % name for name in sorted(MK.expected_constants())
    )


class GathererConstraintTests(unittest.TestCase):
    def assert_valid(self, genome):
        specs = {tmpl: (lo, hi) for tmpl, lo, hi, _ in G.SPEC}
        for phase in G.PHASES:
            values = []
            for resource in G.GATHERERS:
                name = "phase%d-%s" % (phase, resource)
                values.append(genome[name])
                lo, hi = specs["phase{n}-" + resource]
                self.assertGreaterEqual(genome[name], lo)
                self.assertLessEqual(genome[name], hi)
            self.assertEqual(sum(values), 100)

    def test_extreme_and_zero_compositions_respect_each_spec(self):
        cases = ([75, 50, 45, 20], [25, 10, 5, 0], [0, 0, 0, 0])
        for case in cases:
            gene = G.default_genome()
            for phase in G.PHASES:
                for resource, value in zip(G.GATHERERS, case):
                    gene["phase%d-%s" % (phase, resource)] = value
            self.assert_valid(G._normalize(gene))

    def test_random_and_default_genomes_keep_gatherer_bounds(self):
        self.assert_valid(G.default_genome())
        for seed in range(25):
            self.assert_valid(G.random_genome(random.Random(seed)))

    def test_rendered_extreme_genome_values_stay_in_spec(self):
        gene = G.default_genome()
        for phase in G.PHASES:
            for resource, value in zip(G.GATHERERS, (75, 50, 45, 20)):
                gene["phase%d-%s" % (phase, resource)] = value
        rendered = MK.build_per_text(builder_template(), gene, "Bounds")
        values = {name: int(value) for name, value in
                  re.findall(r"\(defconst\s+(phase[1-5]-[\w-]+)\s+(-?\d+)\)", rendered)}
        specs = {tmpl: (lo, hi) for tmpl, lo, hi, _ in G.SPEC}
        for name, value in values.items():
            tmpl = name.replace(name[5], "{n}", 1)
            lo, hi = specs[tmpl]
            self.assertGreaterEqual(value, lo)
            self.assertLessEqual(value, hi)


class AiBuilderCoverageTests(unittest.TestCase):
    def test_all_constants_are_replaced(self):
        gene = G.default_genome()
        result = MK.build_per_text(builder_template(), gene, "Coverage")
        for name, value in gene.items():
            self.assertIn("(defconst %s %d)" % (name, value), result)

    def test_missing_constant_fails_fast(self):
        template = builder_template().replace("(defconst phase1-food-gatherers 0)\n", "")
        with self.assertRaisesRegex(ValueError, "参数集合/版本不匹配"):
            MK.build_per_text(template, G.default_genome(), "Missing")

    def test_unexpected_constant_fails_as_version_mismatch(self):
        template = builder_template() + "\n(defconst phase1-new-parameter 1)"
        with self.assertRaisesRegex(ValueError, "参数集合/版本不匹配"):
            MK.build_per_text(template, G.default_genome(), "VersionMismatch")

    def test_duplicate_definition_fails(self):
        template = builder_template() + "\n(defconst phase1-food-gatherers 1)"
        with self.assertRaisesRegex(ValueError, "必须且只能定义一次"):
            MK.build_per_text(template, G.default_genome(), "Duplicate")

    def test_install_mismatch_writes_no_generated_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            ai_dir = Path(tmp)
            (ai_dir / "base.per").write_text("(defconst phase1-food-gatherers 0)", encoding="latin-1")
            config = {"game": {"ai_dir": str(ai_dir), "base_script": "base.per"}}
            with self.assertRaisesRegex(ValueError, "参数集合/版本不匹配"):
                MK.install("Rejected", G.default_genome(), config)
            self.assertFalse((ai_dir / "EvoAI_Rejected.per").exists())
            self.assertFalse((ai_dir / "EvoAI_Rejected.ai").exists())


if __name__ == "__main__":
    unittest.main()
