// Minimal one-page cover letter.
// Content from the CLI input: typst compile cover.typ out.pdf --input data='<json>'
// Expected shape:
//   {name, contact: {email, phone, linkedin, location}, company, role,
//    body_paragraphs: [str], closing}
#let data = json(bytes(sys.inputs.at("data")))
#let today = datetime.today().display("[day padding:none] [month repr:long] [year]")

#set page(paper: "a4", margin: 2.2cm)
#set text(font: "New Computer Modern", size: 10.5pt, fill: black)
#set par(justify: false, leading: 0.6em)

#text(weight: "bold")[#data.name] \
#{
  let c = data.at("contact", default: (:))
  (
    c.at("email", default: ""),
    c.at("phone", default: ""),
    c.at("linkedin", default: ""),
  ).filter(p => p != "").join("  |  ")
}

#v(1em)
#today

#v(1em)
*Re: Application for #data.role at #data.company*

#v(0.5em)
Dear #data.company Hiring Team,

#v(0.5em)
#for p in data.body_paragraphs [
  #p
  #v(0.6em)
]

#data.at("closing", default: "Sincerely,") \
#data.name
