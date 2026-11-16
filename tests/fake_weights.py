"""Gerçek modelle aynı mimaride, rastgele ağırlıklı küçük dosyalar üretir (yalnız test için).

    python tests/fake_weights.py /tmp/fake   ->  /tmp/fake/cep/{student,decoder}.pt, /tmp/fake/ema/{ema,decoder}.pt
"""
import sys
from pathlib import Path

import torch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engine" / "vendor"))
from ahenk_cep import StudentAcoustic  # noqa: E402
from ema_lightning.decoder import Decoder  # noqa: E402
from ema_lightning.model import Acoustic  # noqa: E402

VOCAB = list(" abcçdefgğhıijklmnoöprsştuüvyzqwxâîû.,!?'-:;\"")
import os
# TINI_FAKE_SIZE=real: gerçek EMA Lightning'e yakın boyut (~5.7M akustik + 3.0M decoder, 4 adım) -> hız testleri için
if os.environ.get("TINI_FAKE_SIZE") == "real":
    CFG = {"d": 208, "n_heads": 4, "n_layers": 6, "distilled": {"times": [0.0, 0.25, 0.5, 0.75]},
           "shared_ada": True, "dur_hidden": 256}
    DEC = {}
else:
    CFG = {"d": 64, "n_heads": 4, "n_layers": 2, "distilled": {"times": [0.0, 0.5]}, "shared_ada": True,
           "dur_hidden": 64}
    DEC = {"ch": 64}


def main(out):
    out = Path(out)
    torch.manual_seed(0)
    (out / "cep").mkdir(parents=True, exist_ok=True)
    (out / "ema").mkdir(parents=True, exist_ok=True)
    import math

    def calibrate(m):  # her harf ~1.39 çerçeve (25 fps) -> saniyede ~18 harf, gerçek modelin konuşma hızı
        with torch.no_grad():
            m.chardur.out.weight.zero_()
            m.chardur.out.bias.fill_(math.log1p(25 / 18))
        return m

    st = calibrate(StudentAcoustic(CFG, VOCAB, True))
    torch.save({"cfg": CFG, "vocab": VOCAB, "ema": st.state_dict()}, out / "cep" / "student.pt")
    ac = calibrate(Acoustic(CFG, VOCAB, True))
    torch.save({"cfg": CFG, "vocab": VOCAB, "ema": ac.state_dict()}, out / "ema" / "ema.pt")
    for d in ("cep", "ema"):
        torch.save({"cfg": DEC, "G": Decoder(**DEC).state_dict()}, out / d / "decoder.pt")
    print("ok", out)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "/tmp/fake")
