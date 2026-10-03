#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Repair research PCM DMA preparation and control, with verification objects.

Adapts public NXP imx-sdma.c (2010 Sascha Hauer/Pengutronix, 2004-2016
Freescale), imx-pcm-dma.c (2009 Sascha Hauer), and pcm_dmaengine.c
(2012 Analog Devices; additional upstream credits retained in generated code).
Apply after the EEG SDMA overlay to a disposable source copy.
"""
import hashlib


def apply(source):
    if (source / '.git').exists():
        raise ValueError('requires disposable non-Git source')
    for name, digest in {
        'sound/core/pcm_dmaengine.c': '95c5d46140355a61d1bb3f1e5966c66ddd4120bc9b7a93bc0e947197818e3bb2',
        'sound/soc/fsl/imx-pcm-dma.c': '5206164b3243fcdf0c5d70f46b84956b892f4a4c76c4804845d7fba243190bef',
    }.items():
        if hashlib.sha256((source / name).read_bytes()).hexdigest() != digest:
            raise ValueError('unexpected PCM source: ' + name)
    path = source / 'drivers/dma/imx-sdma.c'
    code = path.read_text()
    before = '\tif (sdmac->peripheral_type != IMX_DMATYPE_HDMI)\n\t\tnum_periods = buf_len / period_len;'
    if code.count(before) != 1:
        raise ValueError('unexpected cyclic SDMA anchor')
    code = code.replace(before, '''#ifdef CONFIG_DREEM_WM8960
	/* ALSA audio uses this cyclic path, including command 3 for packed
	 * samples. Reject malformed requests before division or allocation.
	 */
	if (sdmac->peripheral_type == IMX_DMATYPE_SAI) {
		if ((direction != DMA_MEM_TO_DEV && direction != DMA_DEV_TO_MEM) ||
		    !period_len || !buf_len || period_len > SDMA_BD_MAX_CNT ||
		    buf_len > INT_MAX || buf_len % period_len ||
		    buf_len / period_len > UINT_MAX / sizeof(struct sdma_buffer_descriptor) ||
		    dma_addr > (dma_addr_t)~0U - (buf_len - 1) ||
		    sdmac->word_size < DMA_SLAVE_BUSWIDTH_1_BYTE ||
		    sdmac->word_size > DMA_SLAVE_BUSWIDTH_4_BYTES ||
		    period_len % sdmac->word_size)
			return NULL;
	}
#endif
''' + before)
    before = '\tsdmac->context_loaded = true;\n\n\treturn ret;'
    if code.count(before) != 1:
        raise ValueError('unexpected SDMA context-result anchor')
    code = code.replace(before, '''#ifdef CONFIG_DREEM_WM8960
	if (sdmac->peripheral_type == IMX_DMATYPE_SAI)
		sdmac->context_loaded = !ret;
	else
#endif
		sdmac->context_loaded = true;

	return ret;''')
    before = '\tif (sdma_load_context(sdmac))\n\t\tgoto err_desc_out;'
    if code.count(before) != 1:
        raise ValueError('unexpected SDMA initialization unwind anchor')
    code = code.replace(before, '''	if (sdma_load_context(sdmac)) {
#ifdef CONFIG_DREEM_WM8960
		if (sdmac->peripheral_type == IMX_DMATYPE_SAI)
			sdma_free_bd(desc);
#endif
		goto err_desc_out;
	}''')
    path.write_text(code)
    path = source / 'sound/soc/fsl/imx-pcm-dma.c'
    code = path.read_text()
    before = '\t.period_bytes_max = 65535, /* Limited by SDMA engine */'
    if code.count(before) != 1:
        raise ValueError('unexpected PCM period limit anchor')
    code = code.replace(before, '''#ifdef CONFIG_DREEM_WM8960
	.period_bytes_max = 65532, /* SDMA_BD_MAX_CNT in imx-sdma.c */
#else
''' + before + '\n#endif')
    path.write_text(code)
    path = source / 'sound/core/pcm_dmaengine.c'
    code = path.read_text()
    before = '\tunsigned long flags = DMA_CTRL_ACK;'
    if code.count(before) != 1:
        raise ValueError('unexpected PCM submission declaration')
    code = code.replace(before, before + '''
#ifdef CONFIG_DREEM_WM8960
	dma_cookie_t cookie;
#endif''')
    before = '\n\tprtd->pos = 0;\n'
    if code.count(before) != 1:
        raise ValueError('unexpected PCM position initialization')
    code = code.replace(before, '\n#ifndef CONFIG_DREEM_WM8960' + before + '#endif\n')
    before = '\tprtd->cookie = dmaengine_submit(desc);'
    if code.count(before) != 1:
        raise ValueError('unexpected PCM submission anchor')
    code = code.replace(before, '''#ifdef CONFIG_DREEM_WM8960
	cookie = dmaengine_submit(desc);
	if (dma_submit_error(cookie))
		return cookie;
	prtd->cookie = cookie;
	prtd->pos = 0;
#else
''' + before + '\n#endif')
    start = code.index('int snd_dmaengine_pcm_trigger(')
    end = code.index('EXPORT_SYMBOL_GPL(snd_dmaengine_pcm_trigger);', start)
    code = code[:start] + '''#ifdef CONFIG_DREEM_WM8960
int snd_dmaengine_pcm_trigger(struct snd_pcm_substream *substream, int cmd)
{
	struct dmaengine_pcm_runtime_data *prtd = substream_to_prtd(substream);
	int ret;

	switch (cmd) {
	case SNDRV_PCM_TRIGGER_START:
		ret = dmaengine_pcm_prepare_and_submit(substream);
		if (ret)
			return ret;
		dma_async_issue_pending(prtd->dma_chan);
		return 0;
	case SNDRV_PCM_TRIGGER_RESUME:
	case SNDRV_PCM_TRIGGER_PAUSE_RELEASE:
		return dmaengine_resume(prtd->dma_chan);
	case SNDRV_PCM_TRIGGER_SUSPEND:
		if (substream->runtime->info & SNDRV_PCM_INFO_PAUSE)
			return dmaengine_pause(prtd->dma_chan);
		return dmaengine_terminate_all(prtd->dma_chan);
	case SNDRV_PCM_TRIGGER_PAUSE_PUSH:
		return dmaengine_pause(prtd->dma_chan);
	case SNDRV_PCM_TRIGGER_STOP:
		return dmaengine_terminate_all(prtd->dma_chan);
	default:
		return -EINVAL;
	}
}
#else
''' + code[start:end] + '#endif\n' + code[end:]
    path.write_text(code)
    for folder, name in (('sound/core', 'pcm_dmaengine.o'),
                         ('sound/soc/fsl', 'imx-pcm-dma.o')):
        with (source / folder / 'Makefile').open('a') as stream:
            stream.write('\nifeq ($(CONFIG_DREEM_WM8960),y)\nCFLAGS_' + name + ' += -g\nendif\n')
