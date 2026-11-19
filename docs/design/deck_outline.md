# Deck outline: how BORG_ROUTE works

Written 15-Nov-2026 by the planner for work order 6.1. Nine slides. Audience: an owner or IT lead at a company that wants AI on its own machines, and the technical person they forward it to. They have two minutes and no background in agent systems.

## Update, 19-Nov-2026: the story the deck tells (planner rulings 44 and 45)

The deck now matches the LinkedIn post the owner approved on 19-Nov-2026 (his ruling 16). Its story is a measured trade-off, with no claim of a win beyond what the README table "What was measured" says. Routing: the planner names a model for each step from the pool's scorecard; when it names none, the step goes to the cheapest model that is good enough, local or cloud, counting the token price and the waiting time (ruling 46). In round 3 the planner named the model for every step except the document job's two. The private lane is a setting you turn on and appears on the building slide only. The first cut's "local models go first" and its local run of 200 emails, which never happened, are gone.

- **The hook stays: "Your AI should come with a receipt."** The receipt on the cover and the receipt slide is a real run: round 3, the email job, the run with the middle cost (A_borg_cloud_3), with the one-call cost of the top model under it.
- **The office image and the offer stay.** The offer slide keeps its two bracketed links until the owner makes the repository public; no PDF export and nothing shared before then.
- **Numbers and limits** carry the round-3 table and its losses: one win by the rule (job B, on input that was not fresh), none in rounds 1 and 2.

Slide order: cover, problem, idea, office, flow, receipt, why, building, numbers, limits, offer. Every figure on a slide carries a claim marker checked by `scripts/check_claims.py` and its claim id in the slide notes. Figures are rounded against BORG_ROUTE.

The why slide shows the labelling step's real route record: the planner named the model, and the cheapest model that cleared the bar was not used (ruling 46).

What 6.1 still has to do: the two links on the offer slide after the switch to public, a browser check of every slide and the PDF export.

Voice: the owner's voice rules apply to every word (no em dashes, plain words, Canadian spelling, no Oxford comma, each idea said once).

## Rules for the whole deck

- One idea per slide. A title that is a full statement, one picture, at most 25 words of body text.
- Plain words. "Step", not "node". "Planner", not "orchestrator". "On your machines", not "on-prem inference".
- Every number comes from `bench/results/<date>/summary.md` and carries its claim id in the slide notes. A placeholder like `{A.cost_ratio}` below means "take this from the results". If a result is unfavourable, the slide says so; do not drop the slide and do not pick a kinder number.
- If Check 3 failed (no job won), stop: slides 5 and 6 need the planner.
- Same tokens as the interface: teal for structure, gold only for the one point the eye should land on, light background. System fonts.
- The same nine slides are the video's shot list, so each must make sense when read aloud in ten seconds.
- No claim about privacy beyond the lane's exact promise: "Raw document text is sent only to models on your own machines. Everything sent to a cloud model is listed, and you can read it."

## Slides

### 1. Most companies send every AI job to one big model
- **Says:** the easy job and the hard job get the same expensive model, and every document goes to someone else's servers.
- **Picture:** a stack of varied jobs (sort 200 emails, write a function, decide a strategy) all flowing through one large box labelled "one top model", with a bill and a "leaves your office" arrow.
- **Notes:** no numbers yet. This is the status quo the audience recognises.

### 2. A project manager would not do that
- **Says:** BORG_ROUTE is a project manager, not a worker. It breaks the job into steps and gives each step to the cheapest model that can do it well.
- **Picture:** the same job split into three steps, each going to a different-sized box: small local, mid cloud, top cloud.
- **Notes:** this is the one sentence people should repeat. Claim: per-step routing (claim test `tests/e2e/test_claim_routing.py`).

### 3. One real job, start to finish
- **Says:** "Classify 200 support emails and summarise the urgent ones" becomes: plan, classify each email, count, summarise, write the answer.
- **Picture:** the Run view's plan diagram from the replay of job A (a screenshot from `docs/design/screens/`), with the step names readable.
- **Notes:** in the video this is the replay playing. Name the model that did each step.

### 4. Every answer comes with a receipt
- **Says:** you see which model did each step, why it was chosen, what it cost and how long it took.
- **Picture:** the receipt under the answer, and the "why this model" table for one step.
- **Notes:** the "same steps on the top model" figure on the receipt is an estimate and is labelled so; do not quote it as a saving. Measured savings are on slide 5.

### 5. Measured: what the same jobs cost both ways
- **Says:** four real jobs, each run through BORG_ROUTE and straight to one top model, three times each.
- **Picture:** one horizontal bar pair per job (direct against BORG_ROUTE), cost on the axis, with the quality result written beside each pair: `{A.cost_direct}` against `{A.cost_borg}`, accuracy `{A.acc_direct}` against `{A.acc_borg}`; likewise B and C. Job D is on slide 7.
- **Notes:** state the win rule that was fixed before the runs. Give medians. Say how many repeats. Claim ids for every figure.

### 6. Where it does not pay
- **Says:** for a short question, routing costs more than asking one model directly: `{short_prompt.ratio}` times more in our measurement. BORG_ROUTE answers those directly and saves its planning for jobs with many steps.
- **Picture:** a simple line: cost against job size, the two lines crossing at the measured break-even; the region left of the crossing shaded "ask directly".
- **Notes:** this slide is what makes the others believable. Use the measured short-prompt figure and the jobs that lost, if any. If the break-even was not measured, show the lost and won jobs as points instead of a line.

### 7. Your documents stay on your machines
- **Says:** the exact promise: "Raw document text is sent only to models on your own machines. Everything sent to a cloud model is listed, and you can read it."
- **Picture:** the Run view of job D: local steps marked with the filled teal dot, and the "What left this machine" list.
- **Notes:** give job D's measured accuracy and cost against the all-cloud run: `{D.acc_local}` against `{D.acc_cloud}`, `{D.cost_local}` against `{D.cost_cloud}`. Say the limit plainly: what a local step returns can be sent on, and the list shows it. Mention the stricter mode that sends nothing out. Claim: privacy test `tests/e2e/test_privacy_lane.py`.

### 8. Try it in two minutes
- **Says:** one command starts a replay of these recorded runs. No key, no cost. Add a key and a local model to run your own jobs.
- **Picture:** the one command (`docker compose up`), then the Ask screen in replay mode.
- **Notes:** the GitHub address goes here once the repo is public (the owner's word). Until then leave the line "Link: to be added at release".

### 9. Built by Aliud Inquisito
- **Says:** AI strategy and architecture for companies that want AI to work on their terms. Dhananjay Raol, Toronto.
- **Picture:** the Aliud Inquisito logo (brand kit path is in the owner's brand notes; use the full logo SVG), the tagline "AI strategy · architecture", contact line.
- **Notes:** one sentence on what a first conversation looks like: "Bring one job you run every week; we will show what it costs each way." No other selling.

## Video script (work order 6.1 writes `video_script.md` from this)

60 to 90 seconds, screen recording of the replay demo with narration. Shots map to slides 1, 2, 3 (replay playing), 4, 5, 7, 8. Slide 6's point is one spoken line over slide 5. Each shot: what is on screen, the narration line (one sentence, under 20 words), duration.
