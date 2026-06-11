// ATS-parser-safe single-column resume.
// All content comes from the CLI input:
//   typst compile resume.typ out.pdf --input data='<json>'
// Expected shape:
//   {name, headline, contact: {email, phone, linkedin, location}, summary,
//    skills: [{label, items: [str]}], experience: [{company, title, dates, bullets: [str]}],
//    projects: [{name, stack, dates, bullets: [str]}],
//    education: [{school, degree, detail, dates}], awards: [str]}
#let data = json(bytes(sys.inputs.at("data")))

#set page(paper: "a4", margin: 1.5cm)
#set text(font: "New Computer Modern", size: 10pt, fill: black)
#set par(justify: false, leading: 0.5em)
#set list(indent: 0.5em, body-indent: 0.4em, spacing: 0.45em)

#let section(title) = {
  v(0.7em)
  text(weight: "bold", size: 10.5pt)[#upper(title)]
  v(0.35em)
}

// ── Header ─────────────────────────────────────────────────────────────
#align(center)[
  #text(size: 15pt, weight: "bold")[#data.name] \
  #data.at("headline", default: "") \
  #{
    let c = data.at("contact", default: (:))
    (
      c.at("email", default: ""),
      c.at("phone", default: ""),
      c.at("linkedin", default: ""),
      c.at("location", default: ""),
    ).filter(p => p != "").join("  |  ")
  }
]

// ── Summary ────────────────────────────────────────────────────────────
#section("Summary")
#data.summary

// ── Skills ─────────────────────────────────────────────────────────────
#section("Skills")
#for g in data.skills [
  *#g.label:* #g.items.join(", ") \
]

// ── Experience ─────────────────────────────────────────────────────────
#section("Experience")
#for (i, job) in data.experience.enumerate() [
  #if i > 0 [ #v(0.45em) ]
  *#job.title* — #job.company #h(1fr) #job.dates \
  #for b in job.bullets [
    - #b
  ]
]

// ── Projects ───────────────────────────────────────────────────────────
#if data.at("projects", default: ()).len() > 0 [
  #section("Projects")
  #for (i, p) in data.projects.enumerate() [
    #if i > 0 [ #v(0.45em) ]
    *#p.name*#if p.at("stack", default: "") != "" [ — #p.stack ] #h(1fr) #p.dates \
    #for b in p.bullets [
      - #b
    ]
  ]
]

// ── Education & Awards ─────────────────────────────────────────────────
#section("Education & Awards")
#for ed in data.education [
  *#ed.school* — #ed.degree#if ed.at("detail", default: "") != "" [ (#ed.detail)] #h(1fr) #ed.dates \
]
#for a in data.at("awards", default: ()) [
  - #a
]
