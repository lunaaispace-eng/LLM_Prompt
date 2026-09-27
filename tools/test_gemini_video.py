"""Offline transport tests. Run with ComfyUI's Python (google-genai installed)."""
import ast
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from google.genai import types
from gemini_video import gemini_video_part


class ExportVideo:
    def save_to(self, path, **kwargs):
        self.path = Path(path)
        self.options = kwargs
        self.path.write_bytes(b"fake-video-for-transport-test")


class VideoTransportTests(unittest.TestCase):
    def client(self, state="ACTIVE"):
        client = Mock()
        client.files.upload.return_value = SimpleNamespace(
            name="files/test", uri="https://example.test/video", state=state)
        return client

    def test_native_part_and_cleanup(self):
        client, video = self.client(), ExportVideo()
        with gemini_video_part(client, types, video, 2.5, 30) as part:
            self.assertEqual(part.video_metadata.fps, 2.5)
            self.assertEqual(part.file_data.mime_type, "video/mp4")
            self.assertFalse(video.path.exists())
            self.assertEqual(video.options, {"format": "mp4", "codec": "h264"})
        client.files.delete.assert_called_once_with(name="files/test")

    def test_generation_failure_still_cleans_up(self):
        client = self.client()
        with self.assertRaisesRegex(RuntimeError, "generation failed"):
            with gemini_video_part(client, types, ExportVideo(), 2, 30):
                raise RuntimeError("generation failed")
        client.files.delete.assert_called_once()

    def test_processing_failure_and_timeout(self):
        for state, timeout, error in [("FAILED", 30, RuntimeError), ("PROCESSING", 0, TimeoutError)]:
            client = self.client(state)
            with self.assertRaises(error):
                with gemini_video_part(client, types, ExportVideo(), 2, timeout):
                    self.fail("Must not generate before ACTIVE")
            client.files.delete.assert_called_once()

    def test_processing_to_active(self):
        client = self.client("PROCESSING")
        client.files.get.return_value = SimpleNamespace(
            name="files/test", uri="https://example.test/video", state="ACTIVE")
        with patch("gemini_video.time.sleep"):
            with gemini_video_part(client, types, ExportVideo(), 2, 30):
                pass
        client.files.get.assert_called_once_with(name="files/test")

    def test_invalid_input_never_uploads(self):
        for video, fps in [(object(), 2), (ExportVideo(), 0), (ExportVideo(), float("nan"))]:
            client = self.client()
            with self.assertRaises(ValueError):
                with gemini_video_part(client, types, video, fps, 30):
                    self.fail("Invalid input accepted")
            client.files.upload.assert_not_called()

    def test_execute_provider_routing(self):
        # Exercise execute itself, isolating unrelated ComfyUI/model imports.
        source = Path(__file__).resolve().parents[1] / "llm_prompt_api_node.py"
        tree = ast.parse(source.read_text(encoding="utf-8"))
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "LLMPromptAPINode")
        execute = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "execute")
        execute.decorator_list = []
        # Stop immediately after the media preparation block.
        stop = next(i for i, n in enumerate(execute.body) if isinstance(n, ast.AnnAssign) and getattr(n.target, "id", "") == "messages")
        execute.body = execute.body[:stop] + [ast.Return(ast.Tuple(elts=[ast.Name(id="native_video", ctx=ast.Load()), ast.Name(id="images_b64", ctx=ast.Load())], ctx=ast.Load()))]
        # Only routing and media conversion are relevant here.
        first = next(i for i,n in enumerate(execute.body) if isinstance(n, ast.AnnAssign) and getattr(n.target,"id", "") == "images_b64")
        execute.body = execute.body[:6] + execute.body[first:]
        module = ast.fix_missing_locations(ast.Module(body=[execute], type_ignores=[]))
        sample = Mock(return_value=["frame1", "frame2"])
        ns = {"PROVIDERS": {"Gemini": {"native_protocol": "gemini"}, "Custom": {"native_protocol": "compat"}}, "_sample_video_frames": sample, "_tensor_to_base64_png": lambda x, **k: x}
        exec(compile(module, str(source), "exec"), ns)
        import inspect
        fn = ns["execute"]
        args = {n: "" for n,p in inspect.signature(fn).parameters.items() if p.default is inspect.Parameter.empty}
        args.update(cls=None, provider="Gemini", model_name="gemini-3.7-flash", video=ExportVideo())
        native, images = fn(**args)
        self.assertTrue(native)
        self.assertEqual(images, [])
        sample.assert_not_called()
        args.update(provider="Custom", video=SimpleNamespace(get_components=lambda: SimpleNamespace(images="tensor")), video_sample_frames=64)
        native, images = fn(**args)
        self.assertFalse(native)
        self.assertEqual(images, ["frame1", "frame2"])
        sample.assert_called_once_with("tensor", 64)


if __name__ == "__main__":
    unittest.main()
