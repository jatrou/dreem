#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Add opt-in trigger rollback to the research board's direct PCM link.

Preserves the NXP soc-pcm.c/soc.h copyright notices in the private source.
Apply after the board and PCM lifetime overlays. ASRC links remain unchanged.
"""
import hashlib
from pathlib import Path

from apply_pcm_lifetime_overlay import replace


def apply(source):
    if (source / '.git').exists():
        raise ValueError('requires disposable non-Git source')
    for name, digest in {
        'sound/soc/soc-pcm.c': '583ae30b84b2c83d417e81d9e54e403a40f63a705b4582cc63318c792d181f3d',
        'include/sound/soc.h': 'ba3be0c4f199d4b4aa3655b54c7e7483e02c44c557b58bf9c74157733d33fe6c',
    }.items():
        if hashlib.sha256((source / name).read_bytes()).hexdigest() != digest:
            raise ValueError('unexpected ASoC trigger source: ' + name)
    path = source / 'include/sound/soc.h'
    code = path.read_text()
    code = replace(code, '\tbool capture_only;\n};', '''\tbool capture_only;
#ifdef CONFIG_DREEM_WM8960
	/* Direct research PCM link only; does not qualify DPCM/ASRC rollback. */
	bool dreem_trigger_rollback;
#endif
};''')
    path.write_text(code)
    path = source / 'sound/soc/soc-pcm.c'
    code = path.read_text()
    before = 'static int soc_pcm_trigger(struct snd_pcm_substream *substream, int cmd)'
    code = replace(code, before, '#ifdef CONFIG_DREEM_WM8960\n' +
                   (Path(__file__).parent / 'kernel/soc_trigger_dreem.inc').read_text() +
                   '\n#endif\n\n' + before)
    start = code.index(before)
    tail = code[start:]
    anchor = '\tint i, ret;\n\n'
    if tail.count(anchor) < 1:
        raise ValueError('missing ASoC trigger entry')
    tail = tail.replace(anchor, anchor + '''#ifdef CONFIG_DREEM_WM8960
	if (rtd->dai_link->dreem_trigger_rollback)
		return dreem_soc_pcm_trigger(substream, cmd);
#endif

''', 1)
    path.write_text(code[:start] + tail)
    path = source / 'sound/soc/fsl/wm8960_lifetime.inc'
    code = path.read_text()
    before = '\tmemcpy(data->links, imx_wm8960_dai, sizeof(data->links));'
    path.write_text(replace(code, before, before + '\n\tdata->links[0].dreem_trigger_rollback = true;'))
    with (source / 'sound/soc/Makefile').open('a') as stream:
        stream.write('\nifeq ($(CONFIG_DREEM_WM8960),y)\nCFLAGS_soc-pcm.o += -g\nendif\n')
