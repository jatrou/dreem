# SPDX-License-Identifier: GPL-2.0-only
"""Repair SAI configuration and stream ownership in the research audio profile.

Adapts public Freescale/NXP fsl_sai.c/h. With CONFIG_DREEM_WM8960 disabled,
both source files preprocess to the original implementation.
"""
import hashlib
from pathlib import Path

HASHES = {'fsl_sai.c': 'e6bec56e8fc2e61252244edda6288e1c152acd77798396723a3871faf2d7c298',
          'fsl_sai.h': '99d8175a138b82016d0a44028162078a7f2ae342b378f50f229feb4d6d24b4a7'}


def apply(source):
    if (source / '.git').exists():
        raise ValueError('requires disposable non-Git source copy')
    folder = source / 'sound/soc/fsl'
    for name, digest in HASHES.items():
        if hashlib.sha256((folder / name).read_bytes()).hexdigest() != digest:
            raise ValueError('unexpected NXP SAI source: ' + name)
    code = (folder / 'fsl_sai.c').read_text()
    for before, after in (
        ('\tsai->slots = slots;', '''#ifdef CONFIG_DREEM_WM8960
	if (slots != 2 || slot_width < 16 || slot_width > 32)
		return -EINVAL;
	sai->dreem_explicit_slots = true;
#endif
	sai->slots = slots;'''),
        ('\tu32 slot_width = word_width;\n\tint ret;', '''\tu32 slot_width = word_width;
	int ret;
#ifdef CONFIG_DREEM_WM8960
	if (sai->dreem_explicit_slots) {
		if (word_width > sai->slot_width)
			return -EINVAL;
		slot_width = sai->slot_width;
	}
#endif''')):
        if code.count(before) != 1:
            raise ValueError('unexpected SAI anchor')
        code = code.replace(before, after)
    start = code.index('static int fsl_sai_set_dai_tdm_slot(')
    end = code.index('static int fsl_sai_trigger(', start)
    code = (code[:start] + '#ifdef CONFIG_DREEM_WM8960\n#include "sai_parameters.inc"\n#else\n' +
            code[start:end] + '#endif\n\n' + code[end:])
    start = code.index('static irqreturn_t fsl_sai_isr(')
    end = code.index('#ifdef CONFIG_DREEM_WM8960\n#include "sai_parameters.inc"', start)
    code = (code[:start] + '#ifdef CONFIG_DREEM_WM8960\n#include "sai_control.inc"\n#else\n' +
            code[start:end] + '#endif\n\n' + code[end:])
    start = code.index('static int fsl_sai_trigger(')
    end = code.index('static int fsl_sai_startup(', start)
    code = code[:start] + '#ifndef CONFIG_DREEM_WM8960\n' + code[start:end] + '#endif\n\n' + code[end:]
    start = code.index('static int fsl_sai_startup(')
    end = code.index('static const struct snd_soc_dai_ops fsl_sai_pcm_dai_ops', start)
    code = (code[:start] + '#ifdef CONFIG_DREEM_WM8960\n#include "sai_lifetime.inc"\n#else\n' +
            code[start:end] + '#endif\n\n' + code[end:])
    before = '\tsai->pdev = pdev;'
    if code.count(before) != 1:
        raise ValueError('unexpected SAI probe anchor')
    code = code.replace(before, before + '''
#ifdef CONFIG_DREEM_WM8960
	spin_lock_init(&sai->dreem_control_lock);
	mutex_init(&sai->dreem_stream_lock);
#endif''')
    (folder / 'fsl_sai.c').write_text(code)
    (folder / 'sai_lifetime.inc').write_bytes(
        (Path(__file__).parent / 'kernel/sai_lifetime.inc').read_bytes())
    (folder / 'sai_parameters.inc').write_bytes(
        (Path(__file__).parent / 'kernel/sai_parameters.inc').read_bytes())
    (folder / 'sai_control.inc').write_bytes(
        (Path(__file__).parent / 'kernel/sai_control.inc').read_bytes())
    code = (folder / 'fsl_sai.h').read_text()
    before = '\tstruct snd_dmaengine_dai_dma_data dma_params_tx;'
    if code.count(before) != 1:
        raise ValueError('unexpected SAI state anchor')
    code = code.replace(before, before + '''
#ifdef CONFIG_DREEM_WM8960
	bool dreem_explicit_slots;
	struct mutex dreem_stream_lock;
	bool dreem_configured[2];
	u32 dreem_format;
	u32 dreem_rate[2], dreem_width[2], dreem_channels[2];
	struct clk *dreem_owned_mclk[2];
	spinlock_t dreem_control_lock;
	bool dreem_running[2], dreem_orphaned[2];
	int dreem_control_error;
#endif''')
    # The header is also included by the board driver, before its own includes.
    before = 'struct fsl_sai {'
    if code.count(before) != 1:
        raise ValueError('unexpected SAI declaration anchor')
    code = code.replace(before, '#ifdef CONFIG_DREEM_WM8960\n#include <linux/mutex.h>\n#include <linux/spinlock.h>\n#endif\n\n' + before)
    (folder / 'fsl_sai.h').write_text(code)
    with (folder / 'Makefile').open('a') as stream:
        stream.write('\nifeq ($(CONFIG_DREEM_WM8960),y)\nCFLAGS_fsl_sai.o += -g\nendif\n')
