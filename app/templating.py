import os
from zoneinfo import available_timezones

from fastapi.responses import HTMLResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape

from . import db
from .util import describe_term, fmt_ts, money, remaining

HERE = os.path.dirname(__file__)
STATIC_DIR = os.path.join(HERE, "static")

env = Environment(loader=FileSystemLoader(os.path.join(HERE, "templates")),
                  autoescape=select_autoescape(["html"]))
env.globals.update(money=money, fmt_ts=fmt_ts, remaining=remaining, describe_term=describe_term)

# IANA time zones for the dropdowns (region/city names plus UTC; skips legacy aliases)
TIMEZONES = sorted(z for z in available_timezones()
                   if "/" in z and z.split("/")[0] in {"Africa", "America", "Antarctica", "Asia", "Atlantic",
                                                       "Australia", "Europe", "Indian", "Pacific"}) + ["UTC"]
env.globals["timezones"] = TIMEZONES


def _register_extras():
    from . import extras
    env.globals["xs"] = extras


_register_extras()


def render(name: str, status_code: int = 200, **ctx) -> HTMLResponse:
    ctx.setdefault("site_name", db.get("site_name"))
    ctx.setdefault("support_text", db.get("support_text"))
    ctx.setdefault("welcome_text", db.get("welcome_text"))
    from . import branding
    ctx.setdefault("logo_url", branding.url())
    request = ctx.pop("request", None)
    if request is not None:
        ctx.setdefault("csrf", request.session.get("csrf", ""))
    return HTMLResponse(env.get_template(name).render(**ctx), status_code=status_code)
