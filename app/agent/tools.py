"""Tool layer: what Ghost's assistant is allowed to do.

Same pattern as function calling in large chat assistants:
  1. every action is declared with a JSON schema (name, description, typed parameters),
  2. the model only *proposes* a call; this module validates the arguments strictly,
  3. a policy decides whether the call runs now or waits for the person's OK (expensive or destructive),
  4. the executor runs it against the real app (the Backend) and returns a short, truthful result.
The model never touches files or the queue directly.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable


class ToolError(Exception):
    """A call that could not run (bad arguments, no such picture…). Sent back to the model so it can fix itself."""


@dataclass
class Tool:
    name: str
    description: str
    params: dict  # JSON-schema "properties"
    required: list[str] = field(default_factory=list)
    kind: str = "act"  # read | act | gated
    gate: Callable[[dict], bool] | None = None  # extra: only gated when this returns True
    label: Callable[[dict], str] | None = None  # how the action is shown to the person

    def schema(self) -> dict:
        return {"type": "function", "function": {"name": self.name, "description": self.description,
                                                 "parameters": {"type": "object", "properties": self.params, "required": self.required}}}

    def needs_ok(self, args: dict) -> bool:
        return self.kind == "gated" or (self.gate is not None and self.gate(args))

    def describe(self, args: dict) -> str:
        try:
            return self.label(args) if self.label else f"{self.name} {args}"
        except Exception:
            return self.name


def _i(a, k="index"):
    return a.get(k)


S, I, B = {"type": "string"}, {"type": "integer", "minimum": 1}, {"type": "boolean"}
PID = {"type": "string", "description": "Project id. Leave out to use the project that is open now."}

TOOLS: list[Tool] = [
    # ---- read
    Tool("list_projects", "Find the person's projects (newest first). Use 'query' to search by words in the idea or title.",
         {"query": S, "limit": {"type": "integer", "minimum": 1, "maximum": 20}}, kind="read"),
    Tool("get_project", "Read one project: its pictures, shots, song takes, status and what was changed.", {"project_id": PID}, kind="read"),
    # ---- act now (cheap, undoable)
    Tool("open_project", "Show a project on screen.", {"project_id": S}, ["project_id"], label=lambda a: "Open the project"),
    Tool("change_picture", "Edit the SAME picture by an instruction (hair, eyes, gender, clothes, light, background…). "
         "all=true applies it to every picture.", {"project_id": PID, "index": I, "instruction": S, "all": B}, ["index", "instruction"],
         gate=lambda a: bool(a.get("all")), label=lambda a: f"Change {'every picture' if a.get('all') else 'picture ' + str(_i(a))}: {a['instruction']}"),
    Tool("retake_picture", "Make picture again. fresh=false keeps the same person and place (Retake); fresh=true starts over (New take).",
         {"project_id": PID, "index": I, "fresh": B}, ["index"],
         label=lambda a: f"{'New take' if a.get('fresh') else 'Retake'} of picture {_i(a)}"),
    Tool("set_main_look", "Use this picture as the main look that retakes copy.", {"project_id": PID, "index": I}, ["index"],
         label=lambda a: f"Make picture {_i(a)} the main look"),
    Tool("change_shot", "Edit the SAME video shot by an instruction, then remake only that shot. all=true changes every shot.",
         {"project_id": PID, "index": I, "instruction": S, "all": B}, ["index", "instruction"],
         gate=lambda a: bool(a.get("all")), label=lambda a: f"Change {'every shot' if a.get('all') else 'shot ' + str(_i(a))}: {a['instruction']}"),
    Tool("retake_shot", "Make one video shot again (new take).", {"project_id": PID, "index": I}, ["index"],
         label=lambda a: f"New take of shot {_i(a)}"),
    Tool("change_song", "Same song and words, different sound (e.g. more upbeat, male voice). Adds a new take.",
         {"project_id": PID, "take": I, "instruction": S, "amount": {"type": "string", "enum": ["little", "medium", "lot"]}},
         ["take", "instruction"], label=lambda a: f"Change song take {a['take']}: {a['instruction']}"),
    Tool("more_song_takes", "Make one more take of the song.", {"project_id": PID}, label=lambda a: "Make another song take"),
    Tool("rewrite_text", "Rewrite text Ghost wrote, by an instruction. field: picture_prompt (needs index), line or shot_prompt "
         "(needs index), lyrics, style (song style), main_prompt. Saves it; remake the picture/shot afterwards if needed.",
         {"project_id": PID, "field": {"type": "string", "enum": ["picture_prompt", "line", "shot_prompt", "lyrics", "style", "main_prompt"]},
          "index": I, "instruction": S}, ["field", "instruction"],
         label=lambda a: f"Rewrite {a['field'].replace('_', ' ')}{' ' + str(a['index']) if a.get('index') else ''}: {a['instruction']}"),
    Tool("undo", "Undo the last change. target: picture (needs index), shot (needs index) or all (last change made to all).",
         {"project_id": PID, "target": {"type": "string", "enum": ["picture", "shot", "all"]}, "index": I}, ["target"],
         label=lambda a: f"Undo {a['target']}{' ' + str(a['index']) if a.get('index') else ''}"),
    Tool("pause", "Pause the project that is being made.", {"project_id": PID}, label=lambda a: "Pause"),
    Tool("resume", "Continue a paused project (or make it after checking prompts).", {"project_id": PID}, label=lambda a: "Continue"),
    Tool("remember", "Remember for the future. kind=means: when the person writes 'text' they mean 'means'. kind=always: always do 'text'.",
         {"kind": {"type": "string", "enum": ["means", "always"]}, "text": S, "means": S}, ["kind", "text"],
         label=lambda a: f"Remember: {a['text']}" + (f" = {a['means']}" if a.get("means") else "")),
    # ---- need the person's OK first (minutes of work, or cannot be undone)
    Tool("create_project", "Start a NEW project. kind: images | video | music. Use same_as to copy settings of an earlier project.",
         {"kind": {"type": "string", "enum": ["images", "video", "music"]}, "idea": S,
          "count": {"type": "integer", "minimum": 1, "maximum": 8}, "seconds": {"type": "integer", "minimum": 8, "maximum": 600},
          "quality": {"type": "string", "enum": ["fast", "quality"]}, "aspect": {"type": "string", "enum": ["9:16", "4:5", "1:1", "16:9"]},
          "language": S, "same_as": S}, ["kind", "idea"], kind="gated",
         label=lambda a: f"Make new {a['kind']}: “{a['idea']}”" + (f" ({a['count']})" if a.get("count") else "")),
    Tool("upgrade_quality", "Re-make a Fast project in Quality.", {"project_id": PID}, kind="gated", label=lambda a: "Re-make it in Quality"),
    Tool("start_again", "Throw away the results and make the project again from the start.", {"project_id": PID}, kind="gated",
         label=lambda a: "Start this project again"),
    Tool("delete_project", "Delete a project and all its files. Cannot be undone.", {"project_id": S}, ["project_id"], kind="gated",
         label=lambda a: "Delete the project (cannot be undone)"),
]
BY_NAME = {t.name: t for t in TOOLS}


PICTURE = {"change_picture", "retake_picture", "set_main_look"}
SHOT = {"change_shot", "retake_shot"}
SONG = {"change_song", "more_song_takes"}
SWAP = {("images", "change_shot"): "change_picture", ("images", "retake_shot"): "retake_picture",
        ("video", "change_picture"): "change_shot", ("video", "retake_picture"): "retake_shot"}


def schemas(kind: str | None = None) -> list[dict]:
    """Only the tools that make sense for what is open: fewer choices = fewer mistakes and a shorter prompt."""
    drop = set()
    if kind != "images":
        drop |= PICTURE
    if kind != "video":
        drop |= SHOT
    if kind != "music":
        drop |= SONG
    return [t.schema() for t in TOOLS if t.name not in drop]


def repair(name: str, args: dict, kind: str | None) -> str:
    """Fix the commonest small-model slip: 'shot' vs 'picture' for the kind of project that is open."""
    new = SWAP.get((kind or "", name))
    if new == "retake_shot":
        args.pop("fresh", None)
    return new or name


def validate(name: str, args: Any) -> dict:
    """Strict argument check + gentle coercion ("2" -> 2). Raises ToolError with a message the model can act on."""
    t = BY_NAME.get(name)
    if not t:
        raise ToolError(f"Unknown tool '{name}'. Use one of: {', '.join(BY_NAME)}")
    if not isinstance(args, dict):
        raise ToolError("Arguments must be an object")
    out = {}
    for k, v in args.items():
        spec = t.params.get(k)
        if spec is None or v is None or v == "":
            continue  # ignore unknown / empty keys instead of failing
        ty = spec.get("type")
        try:
            if ty == "integer":
                v = int(str(v).strip().lstrip("#"))
                if v < spec.get("minimum", -10**9) or v > spec.get("maximum", 10**9):
                    raise ToolError(f"'{k}' must be between {spec.get('minimum')} and {spec.get('maximum')}")
            elif ty == "boolean":
                v = v if isinstance(v, bool) else str(v).lower() in ("true", "1", "yes")
            elif ty == "string":
                v = " ".join(str(v).split())[:600]
        except (TypeError, ValueError):
            raise ToolError(f"'{k}' must be a {ty}")
        if "enum" in spec and v not in spec["enum"]:
            raise ToolError(f"'{k}' must be one of {spec['enum']}")
        out[k] = v
    missing = [r for r in t.required if r not in out]
    if missing:
        raise ToolError(f"Missing: {', '.join(missing)}")
    if name == "rewrite_text" and out["field"] in ("picture_prompt", "line", "shot_prompt") and "index" not in out:
        raise ToolError("This field needs 'index'")
    if name == "undo" and out["target"] != "all" and "index" not in out:
        raise ToolError("Undo of one picture or shot needs 'index'")
    if name == "remember" and out["kind"] == "means" and not out.get("means"):
        raise ToolError("kind=means needs 'means'")
    return out


def run(backend, name: str, args: dict, project: str | None) -> dict:
    """Execute a validated call. Returns {"ok", "text", ["open"]} — the text is what really happened."""
    fn = getattr(backend, name, None)
    if fn is None:
        raise ToolError(f"'{name}' is not available")
    a = dict(args)
    if "project_id" in BY_NAME[name].params and name not in ("open_project", "delete_project", "create_project"):
        a["project_id"] = a.get("project_id") or project
        if not a["project_id"]:
            raise ToolError("No project is open. Ask which project, or use list_projects.")
    return fn(**a)
