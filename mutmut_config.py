"""Configuration mutmut 2 (lu automatiquement depuis la racine du repo).

Accelere le test de mutation de backend/app/logic/ sans en affaiblir la garantie :
- pre_mutation : chaque mutant ne lance que les tests qui citent son module, plus ceux des
  modules de logique qui l'importent (au lieu de tout tests/unit/ : ~2x moins par mutant).
  Aucun test trouve -> commande par defaut (toute la suite).
- modules_to_mutate : en PR, seuls les modules touches (directement ou via leurs tests) sont
  mutes ; le job nocturne (mutation-testing.yml) continue de tout muter.
"""
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent
LOGIC = ROOT / "backend" / "app" / "logic"
TESTS = ROOT / "tests" / "unit"
RUNNER = "python -m pytest -x --assert=plain -p no:randomly -p no:cacheprovider -m 'unit and not no_mutation'"
# Fichiers dont un changement touche tous les modules : on mute tout.
MUTATE_ALL_IF = ("pyproject.toml", "mutmut_config.py", "backend/app/logic/__init__.py", "backend/app/logic/models.py")


SELF_TEST = "tests/unit/test_mutmut_config.py"  # cite des noms de modules, ne teste pas la logique


def logic_modules() -> list[str]:
    return sorted(p.stem for p in LOGIC.glob("*.py") if p.stem != "__init__")


def _cites(text: str, mod: str) -> bool:
    # backend.app.logic.<mod> | from backend.app.logic import <mod> | logic/<mod>.py
    return re.search(rf"logic(?:\.| import |/){mod}\b", text) is not None


def importers(mod: str) -> set[str]:
    """Modules de logique qui importent `mod` (from .mod import / from . import mod)."""
    pat = re.compile(rf"^\s*from \.(?:{mod}\b| import .*\b{mod}\b)", re.M)
    return {m for m in logic_modules() if m != mod and pat.search((LOGIC / f"{m}.py").read_text())}


def tests_for(mod: str) -> list[str]:
    """Fichiers de tests unitaires qui peuvent tuer un mutant de `mod`."""
    mods = {mod} | importers(mod)
    return sorted(str(t.relative_to(ROOT)) for t in TESTS.glob("test_*.py")
                  if str(t.relative_to(ROOT)) != SELF_TEST and any(_cites(t.read_text(), m) for m in mods))


def modules_to_mutate(changed: list[str]) -> list[str]:
    """Chemins des modules a muter pour une liste de fichiers modifies (relatifs au repo)."""
    if any(c in MUTATE_ALL_IF for c in changed):
        return [f"backend/app/logic/{m}.py" for m in logic_modules()]
    mods = set()
    for c in changed:
        p = pathlib.PurePosixPath(c)
        if c.startswith("backend/app/logic/") and p.suffix == ".py" and p.stem in logic_modules():
            mods.add(p.stem)
        elif c.startswith("tests/unit/") and c != SELF_TEST and (ROOT / c).exists():
            text = (ROOT / c).read_text()
            mods |= {m for m in logic_modules() if _cites(text, m)}
    return [f"backend/app/logic/{m}.py" for m in sorted(mods)]


_tests_cache: dict = {}


def pre_mutation(context):
    mod = pathlib.Path(context.filename).stem
    files = _tests_cache.setdefault(mod, tests_for(mod))
    if files:
        context.config.test_command = f"{RUNNER} {' '.join(files)}"
