/* SPDX-License-Identifier: GPL-2.0-or-later */
/* Adapted from public fsl_sai.c, copyright 2012-2016 Freescale.
 * Checked atomic control and IRQ handling by Dreem contributors, 2026.
 * All helpers below run with dreem_control_lock held unless stated otherwise.
 * The MMIO regmap uses fast_io and only atomic clock enable/disable calls.
 */
#define DREEM_SAI_BCE BIT(28)
#define DREEM_SAI_ENABLE (FSL_SAI_CSR_TERE | DREEM_SAI_BCE)
#define DREEM_SAI_REQUEST (FSL_SAI_CSR_FRDE | FSL_SAI_CSR_xIE_MASK)

static int dreem_sai_csr(struct fsl_sai *sai, bool tx, u32 mask, u32 value)
{
	u32 csr;
	int ret = regmap_read(sai->regmap, FSL_SAI_xCSR(tx), &csr);

	if (ret)
		return ret;
	/* Status is W1C or read-only; resets are explicit strobes, not state. */
	csr &= ~(FSL_SAI_CSR_xF_MASK | FSL_SAI_CSR_FR | FSL_SAI_CSR_SR);
	return regmap_write(sai->regmap, FSL_SAI_xCSR(tx),
			    (csr & ~mask) | (value & mask));
}

static bool dreem_sai_needed(struct fsl_sai *sai, bool tx)
{
	return sai->dreem_running[tx] ||
		(sai->dreem_running[!tx] && sai->synchronous[!tx]);
}

static void dreem_sai_error(int *first, int error)
{
	if (!*first && error < 0)
		*first = error;
}

static int dreem_sai_stop_locked(struct fsl_sai *sai, bool tx)
{
	u32 csr, pending = 0, ready = 0;
	int dir, index, first = sai->synchronous[TX] ? RX : TX;
	int count, ret = 0, error;

	sai->dreem_running[tx] = false;
	dreem_sai_error(&ret, dreem_sai_csr(sai, tx, DREEM_SAI_REQUEST, 0));
	/* Keep a clock-providing direction enabled while its peer needs it. */
	for (index = 0; index < 2; ++index) {
		dir = first ^ index;
		if (dreem_sai_needed(sai, dir))
			continue;
		pending |= BIT(dir);
		dreem_sai_error(&ret, dreem_sai_csr(sai, dir,
				DREEM_SAI_ENABLE | DREEM_SAI_REQUEST, 0));
	}
	/* Clear both requested enables before waiting for their final frame. */
	for (count = 0; pending && count < 100; ++count) {
		udelay(10);
		for (dir = TX; dir >= RX; --dir) {
			if (!(pending & BIT(dir)))
				continue;
			error = regmap_read(sai->regmap, FSL_SAI_xCSR(dir), &csr);
			if (error) {
				dreem_sai_error(&ret, error);
				pending &= ~BIT(dir);
			} else if (!(csr & (DREEM_SAI_ENABLE | DREEM_SAI_REQUEST))) {
				pending &= ~BIT(dir);
				ready |= BIT(dir);
			}
		}
	}
	if (pending)
		dreem_sai_error(&ret, -ETIMEDOUT);
	for (dir = TX; dir >= RX; --dir) {
		if (!(ready & BIT(dir)))
			continue;
		/* Reset only after confirmed stop; always attempt to release reset. */
		dreem_sai_error(&ret, regmap_write(sai->regmap,
				FSL_SAI_xCSR(dir), FSL_SAI_CSR_SR));
		dreem_sai_error(&ret, regmap_write(sai->regmap, FSL_SAI_xCSR(dir), 0));
	}
	if (ret)
		sai->dreem_control_error = ret;
	else if (!sai->dreem_running[RX] && !sai->dreem_running[TX])
		sai->dreem_control_error = 0;
	return ret;
}

/* Process-context setup/close callers hold the stream mutex, then this lock. */
static int dreem_sai_stop_stream(struct fsl_sai *sai, bool tx)
{
	unsigned long flags;
	int ret = 0;

	spin_lock_irqsave(&sai->dreem_control_lock, flags);
	if (sai->dreem_running[tx] || sai->dreem_control_error)
		ret = dreem_sai_stop_locked(sai, tx);
	spin_unlock_irqrestore(&sai->dreem_control_lock, flags);
	return ret;
}

static int fsl_sai_trigger(struct snd_pcm_substream *substream, int cmd,
		struct snd_soc_dai *dai)
{
	struct fsl_sai *sai = snd_soc_dai_get_drvdata(dai);
	bool tx = substream->stream == SNDRV_PCM_STREAM_PLAYBACK;
	bool need[2], start;
	unsigned long flags;
	int order[2], i, dir, ret = 0, cleanup;

	switch (cmd) {
	case SNDRV_PCM_TRIGGER_START:
	case SNDRV_PCM_TRIGGER_RESUME:
	case SNDRV_PCM_TRIGGER_PAUSE_RELEASE:
		start = true;
		break;
	case SNDRV_PCM_TRIGGER_STOP:
	case SNDRV_PCM_TRIGGER_SUSPEND:
	case SNDRV_PCM_TRIGGER_PAUSE_PUSH:
		start = false;
		break;
	default:
		return -EINVAL;
	}
	spin_lock_irqsave(&sai->dreem_control_lock, flags);
	if (!start) {
		if (sai->is_stream_opened[tx] || sai->dreem_running[tx] ||
		    sai->dreem_control_error)
			ret = dreem_sai_stop_locked(sai, tx);
		goto out;
	}
	if (!sai->is_stream_opened[tx] || !sai->dreem_configured[tx]) {
		ret = -EBADFD;
		goto out;
	}
	if (sai->dreem_control_error) {
		ret = sai->dreem_control_error;
		goto out;
	}
	if ((sai->synchronous[TX] && sai->synchronous[RX]) ||
	    substream->runtime->channels < 1 || substream->runtime->channels > 2) {
		ret = -EINVAL;
		goto out;
	}
	if (sai->dreem_running[tx])
		goto out;
	if (!sai->dreem_running[!tx]) {
		for (dir = TX; dir >= RX; --dir) {
			ret = dreem_regmap_write_bits(sai->regmap, FSL_SAI_xCR2(dir),
				FSL_SAI_CR2_SYNC, sai->synchronous[dir] ? FSL_SAI_CR2_SYNC : 0);
			if (ret < 0)
				goto rollback;
		}
	}
	for (dir = RX; dir <= TX; ++dir)
		need[dir] = dreem_sai_needed(sai, dir);
	/* Prime before enabling DMA requests, avoiding concurrent CPU/DMA writes. */
	for (i = 0; tx && i < substream->runtime->channels; ++i) {
		ret = regmap_write(sai->regmap, FSL_SAI_TDR, 0);
		if (ret)
			goto rollback;
	}
	if (tx)
		udelay(10);
	/* Enable the dependent direction before its synchronous clock source. */
	order[0] = sai->synchronous[TX] ? TX : RX;
	order[1] = !order[0];
	for (i = 0; i < 2; ++i) {
		dir = order[i];
		if (need[dir] || (dir != tx && !sai->synchronous[tx]))
			continue;
		ret = dreem_sai_csr(sai, dir, DREEM_SAI_ENABLE, DREEM_SAI_ENABLE);
		if (ret)
			goto rollback;
	}
	ret = dreem_sai_csr(sai, tx, DREEM_SAI_REQUEST,
			    FSL_SAI_CSR_FRDE | FSL_SAI_FLAGS);
	if (ret)
		goto rollback;
	sai->dreem_running[tx] = true;
	goto out;
rollback:
	cleanup = dreem_sai_stop_locked(sai, tx);
	if (cleanup)
		dev_err(dai->dev, "SAI start cleanup failed: %d (original %d)\n", cleanup, ret);
out:
	spin_unlock_irqrestore(&sai->dreem_control_lock, flags);
	return ret;
}

static irqreturn_t fsl_sai_isr(int irq, void *devid)
{
	struct fsl_sai *sai = devid;
	unsigned long lock_flags;
	u32 csr, flags, value;
	int dir, ret, error = 0;
	bool handled = false;

	spin_lock_irqsave(&sai->dreem_control_lock, lock_flags);
	for (dir = TX; dir >= RX; --dir) {
		ret = regmap_read(sai->regmap, FSL_SAI_xCSR(dir), &csr);
		if (ret) {
			dreem_sai_error(&error, ret);
			continue;
		}
		flags = csr & ((csr & FSL_SAI_CSR_xIE_MASK) <<
				(FSL_SAI_CSR_xF_SHIFT - FSL_SAI_CSR_xIE_SHIFT));
		if (!flags)
			continue;
		handled = true;
		value = csr & ~(FSL_SAI_CSR_xF_MASK | FSL_SAI_CSR_FR | FSL_SAI_CSR_SR);
		value |= flags & FSL_SAI_CSR_xF_W_MASK;
		if (flags & FSL_SAI_CSR_FEF)
			value |= FSL_SAI_CSR_FR;
		/* FIFO level flags are read-only; this DMA driver cannot service them. */
		value &= ~((flags & (FSL_SAI_CSR_FWF | FSL_SAI_CSR_FRF)) >>
				(FSL_SAI_CSR_xF_SHIFT - FSL_SAI_CSR_xIE_SHIFT));
		ret = regmap_write(sai->regmap, FSL_SAI_xCSR(dir), value);
		dreem_sai_error(&error, ret);
	}
	if (error)
		sai->dreem_control_error = error;
	spin_unlock_irqrestore(&sai->dreem_control_lock, lock_flags);
	return handled ? IRQ_HANDLED : IRQ_NONE;
}
