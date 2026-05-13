# Claude Code default-domain entries not covered by start-agent.sh's allowlist

Source of "Claude Code defaults":

- **`ay4`** (NO_PROXY list, `claude` binary v2.1.140) — hosts that bypass Claude's egress gateway proxy. Not actually merged into `sandbox.network.allowedDomains`, but `api.anthropic.com` and friends must be reachable for the CLI to function.
- **`KcO`** (WebFetch auto-allow set, same binary) — hosts whose WebFetch calls skip the permission prompt. Not merged into `sandbox.network.allowedDomains` either; if the sandbox is active they still need an explicit allow entry.

Source of "my allowlist": the in-script heredoc in `start-agent.sh:337-624`. Coverage is computed under tinyproxy's suffix-match semantics (`anthropic.com` covers `api.anthropic.com`, etc.).

## From `ay4` (NO_PROXY)

Covered already (no action needed): `anthropic.com` family, `registry.npmjs.org`, `pypi.org`, `files.pythonhosted.org`, `index.crates.io`, `proxy.golang.org`.

Not covered:

- `localhost`, `127.0.0.1`, `::1`, `169.254.0.0/16` — local/loopback/link-local literals; sandbox network filter handles these via `allowedHosts` rather than domain matching.
- `jsr.io`
- `npm.jsr.io`

## From `KcO` (WebFetch auto-allow)

Covered already (no action needed): `platform.claude.com`, `code.claude.com`, `developer.mozilla.org`, `go.dev`, `pkg.go.dev`, `www.php.net`, `kotlinlang.org`, `doc.rust-lang.org`, `nodejs.org`, `requests.readthedocs.io`, `cloud.google.com`, `kubernetes.io`, `git-scm.com`.

Not covered:

- `modelcontextprotocol.io`
- `github.com/anthropics` *(path prefix on `github.com`; `start-agent.sh` intentionally omits `github.com` to avoid enabling pushes/PRs at the proxy layer — see `start-agent.sh:350-354`)*
- `agentskills.io`
- `docs.python.org`
- `en.cppreference.com`
- `docs.oracle.com`
- `learn.microsoft.com`
- `docs.swift.org`
- `ruby-doc.org`
- `www.typescriptlang.org`
- `react.dev`
- `angular.io`
- `vuejs.org`
- `nextjs.org`
- `expressjs.com`
- `bun.sh`
- `jquery.com`
- `getbootstrap.com`
- `tailwindcss.com`
- `d3js.org`
- `threejs.org`
- `redux.js.org`
- `webpack.js.org`
- `jestjs.io`
- `reactrouter.com`
- `docs.djangoproject.com`
- `flask.palletsprojects.com`
- `fastapi.tiangolo.com`
- `pandas.pydata.org`
- `numpy.org`
- `www.tensorflow.org`
- `pytorch.org`
- `scikit-learn.org`
- `matplotlib.org`
- `jupyter.org`
- `laravel.com`
- `symfony.com`
- `wordpress.org`
- `docs.spring.io`
- `hibernate.org`
- `tomcat.apache.org`
- `gradle.org`
- `maven.apache.org`
- `asp.net`
- `dotnet.microsoft.com`
- `nuget.org`
- `blazor.net`
- `reactnative.dev`
- `docs.flutter.dev`
- `developer.apple.com`
- `developer.android.com`
- `keras.io`
- `spark.apache.org`
- `huggingface.co` *(`start-agent.sh:391-392` intentionally omits — supports uploads)*
- `www.kaggle.com` *(`start-agent.sh:487-489` intentionally omits data repos — supports uploads)*
- `www.mongodb.com`
- `redis.io`
- `www.postgresql.org`
- `dev.mysql.com`
- `www.sqlite.org`
- `graphql.org`
- `prisma.io`
- `docs.getdbt.com`
- `docs.aws.amazon.com`
- `www.docker.com`
- `www.terraform.io`
- `www.ansible.com`
- `vercel.com/docs` *(path prefix on `vercel.com`)*
- `docs.stripe.com`
- `docs.netlify.com`
- `devcenter.heroku.com`
- `cypress.io`
- `selenium.dev`
- `docs.unity.com`
- `docs.unrealengine.com`
- `nginx.org`
- `httpd.apache.org`

## Notes

- Several of the "not covered" entries are *intentional* omissions in `start-agent.sh` — flagged inline above. Decide per-entry whether to add to the seeded allowlist or accept the gap.
- `github.com/anthropics` and `vercel.com/docs` carry path prefixes that Claude sandbox's host-only matcher cannot enforce. Adding the bare host (`github.com`, `vercel.com`) would allow the host wholesale.
