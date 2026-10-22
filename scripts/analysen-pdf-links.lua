-- Relative Links (z. B. job.yaml, auswertung.py) zeigen im PDF ins Leere.
-- Sie werden auf GitHub umgebogen: <basis>/<pfad>, Basis aus Metadatum link-basis.
local basis

function Meta(meta)
  if meta["link-basis"] then
    basis = pandoc.utils.stringify(meta["link-basis"])
  end
end

function Link(link)
  local ziel = link.target
  if basis and not ziel:match("^%a[%w+.-]*:") and not ziel:match("^#") then
    link.target = basis .. ziel:gsub("^%./", "")
    return link
  end
end

return { { Meta = Meta }, { Link = Link } }
