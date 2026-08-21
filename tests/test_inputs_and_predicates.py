import base64
import http.client
import json
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import MIMI_dashboard
from MIMI_dashboard import LiteratureExtractionRegistry
from literature_to_predicates import (
    SCHEMA_VERSION,
    ExtractionAbandoned,
    PaperPredicateExtraction,
    RankedPredicateOrder,
    add_extraction_to_database,
    collect_pdf_paths,
    extract_pdf,
    extract_pdfs_to_database,
    extraction_prompt,
    main,
    new_database,
    rank_predicates_by_task,
)
from MIMI_inputs import MIMIInputBundle


def sample_extraction() -> PaperPredicateExtraction:
    return PaperPredicateExtraction.model_validate(
        {
            "source": {
                "title": "Seal test",
                "authors": ["A. Researcher"],
                "publication_year": 2025,
                "doi": None,
                "publication_venue": "Example Journal",
            },
            "predicates": [
                {
                    "fact": "Leakage increased with pressure in the tested seal.",
                    "equation": "\\dot{m} = C_d A \\sqrt{2 \\rho \\Delta p}",
                    "variables": [
                        {"symbol": "\\dot{m}", "definition": "mass-flow rate [kg/s]"},
                        {"symbol": "C_d", "definition": "discharge coefficient"},
                        {"symbol": "A", "definition": "clearance area [m^2]"},
                        {"symbol": "\\rho", "definition": "fluid density [kg/m^3]"},
                        {"symbol": "\\Delta p", "definition": "pressure difference [Pa]"},
                    ],
                    "assumptions": [
                        "The seal uses the tested geometry.",
                        "The pressure remains within 1-5 bar.",
                        "The working fluid is water at room temperature.",
                    ],
                    "sources": [
                        {
                            "page": "8",
                            "section": "3.2",
                            "equation": "Eq. (4)",
                            "figure": None,
                            "table": None,
                        }
                    ],
                }
            ],
        }
    )


class BackgroundInputTests(unittest.TestCase):
    def test_json_background_is_validated_and_identified_in_prompt(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spec = root / "spec.md"
            background = root / "knowledge.json"
            spec.write_text("Build a seal tool.", encoding="utf-8")
            background.write_text(
                '{"schema_version":"mimi.predicates.simple.v1",'
                '"predicates":{"P1":{"fact":"x","assumptions":[]}}}',
                encoding="utf-8",
            )
            bundle = MIMIInputBundle(spec_path=spec, background_path=background)
            rendered = bundle.load_background()
            self.assertIn("STRUCTURED BACKGROUND KNOWLEDGE (JSON)", rendered)
            self.assertIn('"fact": "x"', rendered)
            self.assertIn("Apply a fact or equation only when its listed assumptions hold", rendered)

    def test_invalid_json_background_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spec = root / "spec.md"
            background = root / "knowledge.json"
            spec.write_text("Build a seal tool.", encoding="utf-8")
            background.write_text('{"claims":', encoding="utf-8")
            bundle = MIMIInputBundle(spec_path=spec, background_path=background)
            with self.assertRaisesRegex(ValueError, "Background JSON is invalid"):
                bundle.load_background()

    def test_dashboard_preserves_and_validates_json_background(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            payload = {
                "runLevel": 1,
                "spec": {"name": "spec.md", "text": "Build a seal tool."},
                "background": {
                    "name": "seal_predicates.json",
                    "text": '{"schema_version":"mimi.predicates.simple.v1","predicates":{}}',
                },
                "models": {},
                "settings": {},
            }
            with patch.object(MIMI_dashboard, "PROJECT_ROOT", root):
                bundle = MIMI_dashboard.save_uploaded_bundle(payload)
            self.assertEqual(bundle.background_path.suffix, ".json")
            saved = json.loads(bundle.background_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["schema_version"], "mimi.predicates.simple.v1")

    def test_dashboard_passes_review_and_recovery_limits_to_runtime(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            payload = {
                "runLevel": 1,
                "spec": {"name": "spec.md", "text": "Build a seal tool."},
                "maxSubtaskAttempts": 7,
                "maxPlanRevisions": 2,
                "models": {},
                "settings": {},
            }
            with patch.object(MIMI_dashboard, "PROJECT_ROOT", root):
                bundle = MIMI_dashboard.save_uploaded_bundle(payload)

            self.assertEqual(bundle.max_subtask_attempts, 7)
            self.assertEqual(bundle.max_plan_revisions, 2)


class PredicateExtractionStructureTests(unittest.TestCase):
    def test_extraction_prompt_is_general_without_a_task_spec(self):
        prompt = extraction_prompt(
            focus="seal design",
            research_question=None,
            max_predicates=20,
        )
        self.assertNotIn("TASK SPECIFICATION TO SUPPORT", prompt)
        self.assertIn("Prefer facts that affect design choices", prompt)

    def test_extraction_prompt_uses_task_spec_only_as_relevance_context(self):
        prompt = extraction_prompt(
            focus="seal design",
            research_question=None,
            max_predicates=20,
            task_spec="Build a calculator for leakage through a stepped labyrinth seal.",
            task_spec_name="leakage_tool.md",
            reference_image_names=["target_geometry.png"],
        )
        self.assertIn("TASK SPECIFICATION TO SUPPORT (leakage_tool.md)", prompt)
        self.assertIn("REFERENCE IMAGES TO INTERPRET", prompt)
        self.assertIn("target_geometry.png", prompt)
        self.assertIn("topic is not enough", prompt)
        self.assertIn("most to least useful", prompt)
        self.assertIn("controls relevance only", prompt)
        self.assertIn("It is not evidence", prompt)
        self.assertIn("must be supported by the attached paper", prompt)

    def test_api_request_uses_pdf_input_typed_output_and_cleanup(self):
        class FakeFiles:
            def __init__(self):
                self.deleted = []

            def create(self, *, file, purpose):
                self.purpose = purpose
                self.uploaded_bytes = file.read()
                return type("Upload", (), {"id": "file-test"})()

            def delete(self, file_id):
                self.deleted.append(file_id)

        class FakeResponses:
            def parse(self, **kwargs):
                self.kwargs = kwargs
                return type(
                    "Response",
                    (),
                    {"output_parsed": sample_extraction(), "output_text": ""},
                )()

        class FakeClient:
            def __init__(self):
                self.files = FakeFiles()
                self.responses = FakeResponses()

        with tempfile.TemporaryDirectory() as temporary:
            pdf = Path(temporary) / "paper.pdf"
            pdf.write_bytes(b"%PDF-1.4\ntest")
            image = Path(temporary) / "reference.png"
            image.write_bytes(b"\x89PNG\r\n\x1a\nimage")
            client = FakeClient()
            result = extract_pdf(
                client,
                pdf,
                model="test-model",
                focus="seal design",
                research_question=None,
                max_predicates=20,
                detail="high",
                reasoning_effort="medium",
                max_output_tokens=10_000,
                task_spec="Build the pictured seal geometry.",
                reference_image_paths=[image],
            )
            self.assertEqual(result.source.title, "Seal test")
            self.assertEqual(client.files.purpose, "user_data")
            self.assertEqual(client.files.deleted, ["file-test"])
            self.assertIs(client.responses.kwargs["text_format"], PaperPredicateExtraction)
            self.assertFalse(client.responses.kwargs["store"])
            self.assertTrue(client.responses.kwargs["instructions"].startswith("You are"))
            self.assertIn("\\alpha not α", client.responses.kwargs["instructions"])
            file_item = client.responses.kwargs["input"][0]["content"][0]
            self.assertEqual(file_item["file_id"], "file-test")
            self.assertEqual(file_item["detail"], "high")
            image_item = client.responses.kwargs["input"][0]["content"][1]
            self.assertEqual(image_item["type"], "input_image")
            self.assertTrue(image_item["image_url"].startswith("data:image/png;base64,"))
            prompt_item = client.responses.kwargs["input"][0]["content"][2]
            self.assertIn("reference.png", prompt_item["text"])

    def test_database_predicates_use_quantitative_contract(self):
        with tempfile.TemporaryDirectory() as temporary:
            pdf = Path(temporary) / "paper.pdf"
            pdf.write_bytes(b"%PDF-1.4\nminimal test fixture")
            database = new_database(focus="seal design", research_question=None)
            source_id, count = add_extraction_to_database(
                database,
                pdf_path=pdf,
                extraction=sample_extraction(),
                model="test-model",
            )
            self.assertEqual(count, 1)
            self.assertIn(source_id, database["sources"])
            predicate_id, predicate = next(iter(database["predicates"].items()))
            self.assertEqual(
                set(predicate),
                {"fact", "equation", "variables", "assumptions", "sources"},
            )
            self.assertIn("Leakage increased", predicate["fact"])
            self.assertIn("\\sqrt", predicate["equation"])
            self.assertEqual(predicate["variables"]["C_d"], "discharge coefficient")
            self.assertEqual(len(predicate["assumptions"]), 3)
            self.assertEqual(predicate["sources"][0]["source_id"], source_id)
            self.assertEqual(predicate["sources"][0]["equation"], "Eq. (4)")
            self.assertNotIn("predicate_sources", database)
            self.assertEqual(database["sources"][source_id]["extraction_model"], "test-model")

    def test_unicode_mathematics_is_normalized_to_latex(self):
        extraction = PaperPredicateExtraction.model_validate(
            {
                "source": {
                    "title": "Notation test",
                    "authors": [],
                    "publication_year": None,
                    "doi": None,
                    "publication_venue": None,
                },
                "predicates": [
                    {
                        "fact": "The coefficient α varies with μ².",
                        "equation": "α = μ² ± δ",
                        "variables": [
                            {"symbol": "α", "definition": "discharge coefficient"},
                            {"symbol": "μ", "definition": "dynamic viscosity [Pa·s]"},
                            {"symbol": "δ", "definition": "clearance [m]"},
                        ],
                        "assumptions": ["μ ≤ 1."],
                        "sources": [
                            {
                                "page": "4",
                                "section": None,
                                "equation": "Eq. α",
                                "figure": None,
                                "table": None,
                            }
                        ],
                    }
                ],
            }
        )
        with tempfile.TemporaryDirectory() as temporary:
            pdf = Path(temporary) / "paper.pdf"
            pdf.write_bytes(b"%PDF-1.4\ntest")
            database = new_database(focus="seal design", research_question=None)
            add_extraction_to_database(
                database,
                pdf_path=pdf,
                extraction=extraction,
                model="test-model",
            )
        predicate = next(iter(database["predicates"].values()))
        serialized = json.dumps(predicate, ensure_ascii=False)
        self.assertNotRegex(serialized, "[α-ωΑ-Ωµ²±≤·]")
        self.assertEqual(predicate["equation"], "\\alpha = \\mu^{2} \\pm \\delta")
        self.assertIn("\\alpha", predicate["variables"])
        self.assertIn("Pa\\cdot s", predicate["variables"]["\\mu"])

    def test_new_database_uses_quantitative_schema_version(self):
        database = new_database(focus="seal design", research_question=None)
        self.assertEqual(database["schema_version"], SCHEMA_VERSION)

    def test_pdf_collection_supports_directories(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "a.pdf").write_bytes(b"%PDF-1.4\na")
            (root / "ignore.txt").write_text("not a PDF", encoding="utf-8")
            self.assertEqual([path.name for path in collect_pdf_paths([str(root)])], ["a.pdf"])

    def test_shared_extraction_api_builds_database_without_real_api_call(self):
        with tempfile.TemporaryDirectory() as temporary:
            pdf = Path(temporary) / "paper.pdf"
            pdf.write_bytes(b"%PDF-1.4\ntest")
            with patch(
                "literature_to_predicates.extract_pdf",
                return_value=sample_extraction(),
            ) as mocked_extract:
                database, count = extract_pdfs_to_database(
                    [pdf],
                    model="test-model",
                    focus="seal design",
                    client=object(),
                )
            self.assertEqual(count, 1)
            self.assertEqual(len(database["sources"]), 1)
            self.assertEqual(database["extraction_runs"][0]["input_files"], ["paper.pdf"])
            self.assertEqual(database["selection_mode"], "general")
            self.assertIsNone(database["task_specification"])
            self.assertIsNone(mocked_extract.call_args.kwargs["task_spec"])
            mocked_extract.assert_called_once()

    def test_shared_extraction_passes_task_spec_and_records_its_fingerprint(self):
        with tempfile.TemporaryDirectory() as temporary:
            pdf = Path(temporary) / "paper.pdf"
            pdf.write_bytes(b"%PDF-1.4\ntest")
            image = Path(temporary) / "seal.png"
            image.write_bytes(b"\x89PNG\r\n\x1a\nimage")
            task_spec = "Build a leakage calculator using equations reported in the literature."
            with patch(
                "literature_to_predicates.extract_pdf",
                return_value=sample_extraction(),
            ) as mocked_extract:
                database, count = extract_pdfs_to_database(
                    [pdf],
                    model="test-model",
                    focus="seal design",
                    task_spec=task_spec,
                    task_spec_name="calculator.md",
                    reference_image_paths=[image],
                    client=object(),
                )
            self.assertEqual(count, 1)
            self.assertEqual(database["selection_mode"], "task_specific")
            self.assertEqual(database["task_specification"]["name"], "calculator.md")
            self.assertEqual(database["task_specification"]["character_count"], len(task_spec))
            self.assertEqual(database["reference_images"][0]["name"], "seal.png")
            self.assertEqual(
                database["extraction_runs"][0]["reference_images"][0]["name"],
                "seal.png",
            )
            self.assertEqual(
                mocked_extract.call_args.kwargs["task_spec"],
                task_spec,
            )
            self.assertEqual(
                mocked_extract.call_args.kwargs["task_spec_name"],
                "calculator.md",
            )
            self.assertEqual(
                mocked_extract.call_args.kwargs["reference_image_paths"],
                [image.resolve()],
            )

    def test_shared_extraction_hard_limits_model_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            pdf = Path(temporary) / "paper.pdf"
            pdf.write_bytes(b"%PDF-1.4\ntest")
            extraction = sample_extraction()
            extraction = extraction.model_copy(
                update={"predicates": extraction.predicates * 5}
            )
            with patch(
                "literature_to_predicates.extract_pdf",
                return_value=extraction,
            ):
                database, count = extract_pdfs_to_database(
                    [pdf],
                    model="test-model",
                    focus="seal design",
                    task_spec="Build the seal.",
                    max_predicates=2,
                    client=object(),
                )
            self.assertEqual(count, 2)
            self.assertEqual(len(database["predicates"]), 2)

    def test_task_specific_predicates_are_ranked_across_papers(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first_pdf = root / "first.pdf"
            second_pdf = root / "second.pdf"
            first_pdf.write_bytes(b"%PDF-1.4\nfirst")
            second_pdf.write_bytes(b"%PDF-1.4\nsecond")
            first_extraction = sample_extraction()
            first_extraction.predicates[0].fact = "Useful fact from paper one."
            second_extraction = sample_extraction()
            second_extraction.predicates[0].fact = "Most useful fact from paper two."

            def reverse_source_order(client, database, **kwargs):
                return list(reversed(database["predicates"]))

            with (
                patch(
                    "literature_to_predicates.extract_pdf",
                    side_effect=[first_extraction, second_extraction],
                ),
                patch(
                    "literature_to_predicates.rank_predicates_by_task",
                    side_effect=reverse_source_order,
                ) as mocked_rank,
            ):
                database, count = extract_pdfs_to_database(
                    [first_pdf, second_pdf],
                    model="test-model",
                    focus="seal design",
                    task_spec="Build the most accurate seal calculator.",
                    max_predicates=10,
                    client=object(),
                )

            self.assertEqual(count, 2)
            self.assertEqual(
                [item["fact"] for item in database["predicates"].values()],
                ["Most useful fact from paper two.", "Useful fact from paper one."],
            )
            self.assertEqual(
                database["extraction_runs"][0]["predicate_order"],
                "task_relevance_across_all_papers",
            )
            mocked_rank.assert_called_once()

    def test_ranking_request_keeps_all_known_ids_without_duplicates(self):
        class FakeResponses:
            def parse(self, **kwargs):
                self.kwargs = kwargs
                return type(
                    "Response",
                    (),
                    {
                        "output_parsed": RankedPredicateOrder(
                            ordered_predicate_ids=["P2", "unknown", "P2"]
                        ),
                        "output_text": "",
                    },
                )()

        client = type("Client", (), {"responses": FakeResponses()})()
        database = {
            "sources": {
                "S1": {"file_name": "one.pdf", "metadata": {"title": "One"}},
                "S2": {"file_name": "two.pdf", "metadata": {"title": "Two"}},
            },
            "predicates": {
                "P1": {
                    "fact": "First",
                    "equation": None,
                    "variables": {},
                    "assumptions": [],
                    "sources": [{"source_id": "S1"}],
                },
                "P2": {
                    "fact": "Second",
                    "equation": "x=1",
                    "variables": {"x": "value"},
                    "assumptions": [],
                    "sources": [{"source_id": "S2"}],
                },
            },
        }
        ranked = rank_predicates_by_task(
            client,
            database,
            model="test-model",
            task_spec="Build a calculator.",
            task_spec_name="tool.md",
            reference_image_paths=[],
            detail="high",
            reasoning_effort="medium",
            max_output_tokens=10_000,
        )
        self.assertEqual(ranked, ["P2", "P1"])
        self.assertIs(client.responses.kwargs["text_format"], RankedPredicateOrder)
        ranking_prompt = client.responses.kwargs["input"][0]["content"][-1]["text"]
        self.assertIn("freely interleave papers", ranking_prompt)
        self.assertIn('"predicate_id":"P1"', ranking_prompt)

    def test_shared_extraction_discards_an_inflight_result_after_abandon(self):
        with tempfile.TemporaryDirectory() as temporary:
            pdf = Path(temporary) / "paper.pdf"
            pdf.write_bytes(b"%PDF-1.4\ntest")
            abandoned = threading.Event()
            database = new_database(focus="seal design", research_question=None)

            def finish_after_abandon(*args, **kwargs):
                abandoned.set()
                return sample_extraction()

            with patch(
                "literature_to_predicates.extract_pdf",
                side_effect=finish_after_abandon,
            ) as mocked_extract:
                with self.assertRaisesRegex(ExtractionAbandoned, "was abandoned"):
                    extract_pdfs_to_database(
                        [pdf],
                        model="test-model",
                        focus="seal design",
                        client=object(),
                        database=database,
                        should_abandon=abandoned.is_set,
                    )
            mocked_extract.assert_called_once()
            self.assertEqual(database["sources"], {})
            self.assertEqual(database["predicates"], {})

    def test_dashboard_pdf_drop_creates_background_json(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            encoded = base64.b64encode(b"%PDF-1.4\ntest").decode("ascii")
            payload = {
                "pdfs": [{
                    "name": "paper.pdf",
                    "dataUrl": f"data:application/pdf;base64,{encoded}",
                }]
            }
            database = new_database(focus="seal design", research_question=None)
            with (
                patch.object(MIMI_dashboard, "PROJECT_ROOT", root),
                patch.object(
                    MIMI_dashboard,
                    "extract_pdfs_to_database",
                    return_value=(database, 7),
                ) as mocked_extract,
            ):
                result = MIMI_dashboard.extract_uploaded_literature(payload)
            self.assertEqual(result["pdfCount"], 1)
            self.assertEqual(result["predicateCount"], 7)
            self.assertEqual(result["selectionMode"], "general")
            self.assertIsNone(result["taskSpecName"])
            self.assertEqual(result["maxPredicatesPerPaper"], 10)
            self.assertEqual(mocked_extract.call_args.kwargs["max_predicates"], 10)
            self.assertEqual(json.loads(result["text"])["schema_version"], database["schema_version"])
            saved_pdf = mocked_extract.call_args.args[0][0]
            self.assertEqual(saved_pdf.read_bytes(), b"%PDF-1.4\ntest")

    def test_dashboard_passes_uploaded_task_spec_to_extraction(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            encoded = base64.b64encode(b"%PDF-1.4\ntest").decode("ascii")
            encoded_image = base64.b64encode(b"\x89PNG\r\n\x1a\nimage").decode("ascii")
            payload = {
                "pdfs": [{
                    "name": "paper.pdf",
                    "dataUrl": f"data:application/pdf;base64,{encoded}",
                }],
                "taskSpec": {
                    "name": "leakage calculator.md",
                    "text": "Build a leakage calculator from published equations.",
                },
                "referenceImages": [{
                    "name": "seal.png",
                    "dataUrl": f"data:image/png;base64,{encoded_image}",
                }],
                "maxPredicatesPerPaper": 17,
            }
            database = new_database(
                focus="seal design",
                research_question=None,
                task_spec=payload["taskSpec"]["text"],
                task_spec_name="leakage_calculator.md",
            )
            with (
                patch.object(MIMI_dashboard, "PROJECT_ROOT", root),
                patch.object(
                    MIMI_dashboard,
                    "extract_pdfs_to_database",
                    return_value=(database, 5),
                ) as mocked_extract,
            ):
                result = MIMI_dashboard.extract_uploaded_literature(payload)
            self.assertEqual(result["selectionMode"], "task_specific")
            self.assertEqual(result["taskSpecName"], "leakage_calculator.md")
            self.assertEqual(result["referenceImageCount"], 1)
            self.assertEqual(result["maxPredicatesPerPaper"], 17)
            self.assertNotIn("predicateBudget", result)
            self.assertEqual(
                mocked_extract.call_args.kwargs["task_spec"],
                payload["taskSpec"]["text"],
            )
            self.assertEqual(
                mocked_extract.call_args.kwargs["task_spec_name"],
                "leakage_calculator.md",
            )
            self.assertEqual(mocked_extract.call_args.kwargs["max_predicates"], 17)
            reference_path = mocked_extract.call_args.kwargs["reference_image_paths"][0]
            self.assertEqual(reference_path.name, "seal.png")
            self.assertEqual(reference_path.read_bytes(), b"\x89PNG\r\n\x1a\nimage")

    def test_dashboard_rejects_invalid_predicate_limit(self):
        encoded = base64.b64encode(b"%PDF-1.4\ntest").decode("ascii")
        payload = {
            "pdfs": [{
                "name": "paper.pdf",
                "dataUrl": f"data:application/pdf;base64,{encoded}",
            }],
            "maxPredicatesPerPaper": 251,
        }
        with self.assertRaisesRegex(ValueError, "between 1 and 250"):
            MIMI_dashboard.extract_uploaded_literature(payload)

    def test_dashboard_abandon_does_not_write_partial_background(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            encoded = base64.b64encode(b"%PDF-1.4\ntest").decode("ascii")
            payload = {
                "pdfs": [{
                    "name": "paper.pdf",
                    "dataUrl": f"data:application/pdf;base64,{encoded}",
                }]
            }
            abandoned = threading.Event()
            database = new_database(focus="seal design", research_question=None)

            def finish_after_abandon(*args, **kwargs):
                abandoned.set()
                return database, 7

            with (
                patch.object(MIMI_dashboard, "PROJECT_ROOT", root),
                patch.object(
                    MIMI_dashboard,
                    "extract_pdfs_to_database",
                    side_effect=finish_after_abandon,
                ),
            ):
                with self.assertRaisesRegex(ExtractionAbandoned, "was abandoned"):
                    MIMI_dashboard.extract_uploaded_literature(
                        payload,
                        should_abandon=abandoned.is_set,
                    )
            self.assertEqual(list(root.rglob("literature_predicates.json")), [])

    def test_literature_registry_signals_only_the_active_job(self):
        registry = LiteratureExtractionRegistry()
        event = registry.begin("job-1")
        self.assertIsNotNone(event)
        self.assertIsNone(registry.begin("job-2"))
        self.assertFalse(registry.abandon("job-2"))
        self.assertTrue(registry.abandon("job-1"))
        self.assertTrue(event.is_set())
        registry.finish("job-1")
        self.assertFalse(registry.has_active_job())
        self.assertIsNotNone(registry.begin("job-2"))
        registry.finish("job-2")

    def test_literature_abandon_button_is_wired_to_backend_route(self):
        project_root = Path(MIMI_dashboard.__file__).resolve().parent
        html = (project_root / "web" / "index.html").read_text(encoding="utf-8")
        javascript = (project_root / "web" / "app.js").read_text(encoding="utf-8")
        self.assertIn('id="abandon-literature"', html)
        self.assertIn('fetch("/literature-predicates/abandon"', javascript)
        self.assertIn('"X-MIMI-Literature-Job": literatureJobId', javascript)
        self.assertIn("referenceImages", javascript)
        self.assertIn("Reference images", html)
        self.assertIn('id="literature-max-predicates"', html)
        self.assertIn("maxPredicatesPerPaper", javascript)

    def test_handler_abandon_route_signals_the_active_extraction(self):
        started = threading.Event()
        response_result = {}

        def wait_for_abandon(payload, *, should_abandon):
            started.set()
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                if should_abandon():
                    raise ExtractionAbandoned("Literature predicate extraction was abandoned.")
                time.sleep(0.005)
            raise RuntimeError("The abandon signal was not received.")

        handler = MIMI_dashboard.make_web_handler(lambda bundle: None)
        handler.log_message = lambda *args: None
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()

        def request_extraction():
            connection = http.client.HTTPConnection(*server.server_address, timeout=5)
            body = json.dumps({"pdfs": []})
            connection.request(
                "POST",
                "/literature-predicates",
                body=body,
                headers={
                    "Content-Type": "application/json",
                    "X-MIMI-Literature-Job": "job-route-test",
                },
            )
            response = connection.getresponse()
            response_result["status"] = response.status
            response_result["body"] = json.loads(response.read().decode("utf-8"))
            connection.close()

        extraction_thread = threading.Thread(target=request_extraction)
        try:
            with patch.object(
                MIMI_dashboard,
                "extract_uploaded_literature",
                side_effect=wait_for_abandon,
            ):
                extraction_thread.start()
                self.assertTrue(started.wait(1), "Extraction request did not start.")

                connection = http.client.HTTPConnection(*server.server_address, timeout=5)
                body = json.dumps({"jobId": "job-route-test"})
                connection.request(
                    "POST",
                    "/literature-predicates/abandon",
                    body=body,
                    headers={"Content-Type": "application/json"},
                )
                response = connection.getresponse()
                self.assertEqual(response.status, 202)
                response.read()
                connection.close()

                extraction_thread.join(timeout=3)
                self.assertFalse(extraction_thread.is_alive())
        finally:
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=3)
        self.assertEqual(response_result["status"], 409)
        self.assertTrue(response_result["body"]["abandoned"])

    def test_existing_output_is_rejected_before_any_api_request(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pdf = root / "paper.pdf"
            output = root / "knowledge.json"
            pdf.write_bytes(b"%PDF-1.4\ntest")
            output.write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(FileExistsError, "Use --append or --force"):
                main([str(pdf), "--output", str(output)])


if __name__ == "__main__":
    unittest.main()
