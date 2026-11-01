import sys

import matplotlib
import numpy as np

matplotlib.use("Agg")
import matplotlib.pyplot as plt

OUT, OUTH, G = sys.argv[1:4]


def grid(path, col):
    a = np.load(path)
    ids = a[0].astype(int)
    m = np.full(75 * 112, np.nan)
    m[ids] = a[col]
    return m.reshape(75, 112), ids


lat = np.arange(71.5, 34.49, -0.5)
lon = np.arange(-10.5, 45.01, 0.5)


def box(ids):
    i, j = ids // 112, ids % 112
    return i.min(), i.max() + 1, j.min(), j.max() + 1


rows = [("de", "Deutschland"), ("pl", "Polen"), ("at", "Österreich"), ("es", "Spanien")]
fig, ax = plt.subplots(4, 4, figsize=(14, 13))
for r, (c, name) in enumerate(rows):
    sh, ids = grid(f"{OUT}/w_{c}_wind.npy", 2)
    fr, _ = grid(f"{OUT}/w_{c}_wind.npy", 1)
    st, _ = grid(f"{OUTH}/w_{c}_wind.npy", 1)
    panels = [
        (sh / np.nansum(sh), "Flächenmittel (heute)"),
        (fr, "gelernt, schwach geglättet"),
        (st, "gelernt, stark geglättet (λ = 0,3)"),
    ]
    if c == "de":
        mz, _ = grid(f"{OUT}/w_mastr_de_wind.npy", 1)
        panels.append((mz, "MaStR (Referenz)"))
    i0, i1, j0, j1 = box(ids)
    for k in range(4):
        a = ax[r, k]
        if k >= len(panels):
            a.axis("off")
            continue
        m, t = panels[k]
        m = np.where(m > 0, m, np.nan)
        im = a.imshow(
            m[i0:i1, j0:j1],
            extent=[lon[j0] - 0.25, lon[j1 - 1] + 0.25, lat[i1 - 1] - 0.25, lat[i0] + 0.25],
            cmap="viridis",
            vmin=0,
            vmax=np.nanmax(m),
        )
        a.contour(
            lon[j0:j1],
            lat[i0:i1],
            np.isnan(sh[i0:i1, j0:j1]).astype(float),
            [0.5],
            colors="0.4",
            linewidths=0.6,
        )
        a.set_title(f"{name}: {t}", fontsize=9)
        a.tick_params(labelsize=7)
fig.suptitle("Gewichte Wind an Land je GFS-Zelle (0,5°), Anteile je Zone", fontsize=12)
fig.tight_layout()
fig.savefig(f"{G}/gewichte_wind.png", dpi=110)
