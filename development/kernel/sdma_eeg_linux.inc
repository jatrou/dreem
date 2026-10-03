/* SPDX-License-Identifier: GPL-2.0-only */
/* Independent research extension to the pinned NXP driver. No vendor source.
 * Built-in, static DT only. See sdma-provider.md before any device trial. */
#include "sdma_eeg.c"
#include "sdma_acquire_code.h"

#define DREEM_PROGRAM_START 0x1800u
#define DREEM_PROGRAM_END   0x2000u
#define DREEM_SCRIPT_BYTES  1024u
#define DREEM_CHANNEL       1
#define DREEM_EVENT         3
#define DREEM_CONTROL_BYTES 64

u8 *sdma_ads_user_buffer;
int sdma_queue_head;
struct semaphore ads_data_sem = __SEMAPHORE_INITIALIZER(ads_data_sem, 0);
EXPORT_SYMBOL(sdma_ads_user_buffer);
EXPORT_SYMBOL(sdma_queue_head);
EXPORT_SYMBOL(ads_data_sem);

/* This instance and published coherent buffers live until reboot. The target
 * is a built-in static-DT device with bind/unbind attributes suppressed. A
 * defensive remove stops requests and retains DMA storage: STOP_STAT reports
 * enable bits, not proof that an in-flight transfer has completed. */
static struct sdma_engine *dreem_sdma_owner;
static int dreem_sdma_error = -ENODEV;

int dreem_sdma_status(void)
{
	int error = READ_ONCE(dreem_sdma_error);
	if (error)
		return error;
	return READ_ONCE(sdma_ads_user_buffer) ? 0 : -EAGAIN;
}
EXPORT_SYMBOL_GPL(dreem_sdma_status);

static void dreem_sdma_event(struct sdma_engine *sdma, bool enable)
{
	unsigned long flags;
	u32 offset = chnenbl_ofs(sdma, DREEM_EVENT);
	u32 value;
	spin_lock_irqsave(&sdma->eeg.event_lock, flags);
	value = readl(sdma->regs + offset);
	writel(enable ? value | BIT(DREEM_CHANNEL) : value & ~BIT(DREEM_CHANNEL),
	       sdma->regs + offset);
	readl(sdma->regs + offset); /* Complete event masking before publishing STOP. */
	spin_unlock_irqrestore(&sdma->eeg.event_lock, flags);
}

static void dreem_sdma_stop(struct sdma_engine *sdma)
{
	dreem_sdma_event(sdma, false);
	writel(BIT(DREEM_CHANNEL), sdma->regs + SDMA_H_STATSTOP);
	writel(0, sdma->regs + SDMA_CHNPRI_0 + 4 * DREEM_CHANNEL);
}

static void dreem_sdma_command_failed(struct sdma_engine *sdma)
{
	/* Called with the channel-0 lock held. Never reclaim command buffers after
	 * a timeout: the engine may still hold their DMA addresses. */
	spin_lock(&sdma->eeg.progress_lock);
	WRITE_ONCE(sdma->eeg.command_failed, true);
	WRITE_ONCE(sdma->eeg.error, -ETIMEDOUT);
	WRITE_ONCE(dreem_sdma_error, -ETIMEDOUT);
	writel(BIT(0), sdma->regs + SDMA_H_STATSTOP);
	writel(0, sdma->regs + SDMA_CHNPRI_0);
	dreem_sdma_stop(sdma);
	spin_unlock(&sdma->eeg.progress_lock);
}

static void dreem_sdma_notify(void *context, u32 head)
{
	WRITE_ONCE(sdma_queue_head, head);
	/* Publish the index before the semaphore releases the reader. */
	smp_wmb();
	up(&ads_data_sem);
}

static void dreem_sdma_irq(struct sdma_engine *sdma)
{
	struct dreem_sdma *eeg = &sdma->eeg;
	u32 count;
	int ret;
	spin_lock(&eeg->progress_lock);
	if (!READ_ONCE(eeg->armed) || READ_ONCE(eeg->error))
		goto out;
	/* One coherent counter snapshot, then order the accompanying sample data. */
	count = READ_ONCE(*eeg->counter);
	dma_rmb();
	if (eeg->managed && (READ_ONCE(eeg->counter[3]) ||
	    (!eeg->paused && !eeg->pausing &&
	     !(readl(sdma->regs + SDMA_H_EVTPEND) & BIT(DREEM_CHANNEL)))))
		ret = -EIO;
	else
		ret = sdma_eeg_advance(&eeg->progress, count, dreem_sdma_notify, sdma);
	if (ret < 0) {
		WRITE_ONCE(eeg->error, ret);
		WRITE_ONCE(dreem_sdma_error, ret);
		dreem_sdma_stop(sdma);
		up(&ads_data_sem); /* The managed ADC checks status after waking. */
	}
out:
	spin_unlock(&eeg->progress_lock);
}

static int dreem_sdma_clocks_get(struct sdma_engine *sdma)
{
	int ret = clk_enable(sdma->clk_ipg);
	if (ret)
		return ret;
	ret = clk_enable(sdma->clk_ahb);
	if (ret)
		clk_disable(sdma->clk_ipg);
	return ret;
}

static void dreem_sdma_clocks_put(struct sdma_engine *sdma)
{
	clk_disable(sdma->clk_ahb);
	clk_disable(sdma->clk_ipg);
}

/* Validate the external firmware before the base driver dereferences its
 * address table. Instruction addresses count 16-bit words; context RAM takes
 * 0x1000..0x17ff, leaving 0x1800..0x1fff for programs. */
static int dreem_sdma_firmware_bounds(const struct firmware *fw,
				     unsigned int scripts, u32 *next_pc)
{
	const struct sdma_firmware_header *header = (const void *)fw->data;
	const struct sdma_script_start_addrs *addr;
	u32 bytes, table_bytes, start;
	if (fw->size < sizeof(*header))
		return -EINVAL;
	table_bytes = max_t(u32, scripts * sizeof(s32),
			offsetof(struct sdma_script_start_addrs, ram_code_start_addr) + sizeof(s32));
	if ((header->script_addrs_start & 3) ||
	    header->script_addrs_start < sizeof(*header) ||
	    header->script_addrs_start > fw->size ||
	    table_bytes > fw->size - header->script_addrs_start ||
	    (header->ram_code_start & 1) ||
	    header->ram_code_start < header->script_addrs_start + table_bytes ||
	    header->ram_code_start > fw->size ||
	    header->num_script_addrs < scripts)
		return -EINVAL;
	bytes = header->ram_code_size;
	if (!bytes || (bytes & 1) || bytes > fw->size - header->ram_code_start)
		return -EINVAL;
	addr = (const void *)(fw->data + header->script_addrs_start);
	start = addr->ram_code_start_addr;
	if (start < DREEM_PROGRAM_START || start >= DREEM_PROGRAM_END ||
	    bytes / 2 > DREEM_PROGRAM_END - start)
		return -EINVAL;
	*next_pc = start + bytes / 2;
	return 0;
}

static void dreem_sdma_load_firmware(const struct firmware *fw, struct sdma_engine *sdma)
{
	const struct sdma_firmware_header *header;
	const struct sdma_script_start_addrs *addr;
	u32 next_pc = 0, scripts;
	int ret = 0;
	mutex_lock(&sdma->eeg.lock);
	if (!fw) {
		sdma->eeg.pc = DREEM_PROGRAM_START;
		goto done;
	}
	if (fw->size < sizeof(*header)) {
		ret = -EINVAL;
		goto done;
	}
	header = (const void *)fw->data;
	if (header->magic != SDMA_FIRMWARE_MAGIC) {
		ret = -EINVAL;
		goto done;
	}
	switch (header->version_major) {
	case 1: scripts = SDMA_SCRIPT_ADDRS_ARRAY_SIZE_V1; break;
	case 2: scripts = SDMA_SCRIPT_ADDRS_ARRAY_SIZE_V2; break;
	case 3: scripts = SDMA_SCRIPT_ADDRS_ARRAY_SIZE_V3; break;
	case 4: scripts = SDMA_SCRIPT_ADDRS_ARRAY_SIZE_V4; break;
	default: ret = -EINVAL; goto done;
	}
	ret = dreem_sdma_firmware_bounds(fw, scripts, &next_pc);
	if (ret)
		goto done;
	addr = (const void *)(fw->data + header->script_addrs_start);
	ret = dreem_sdma_clocks_get(sdma);
	if (ret)
		goto done;
	ret = sdma_load_script(sdma, (void *)(fw->data + header->ram_code_start),
			       header->ram_code_size, addr->ram_code_start_addr);
	if (sdma->eeg.command_failed)
		sdma->eeg.clocks_held = true;
	else
		dreem_sdma_clocks_put(sdma);
	if (ret)
		goto done;
	sdma->script_number = scripts;
	sdma_add_scripts(sdma, addr);
	sdma->eeg.pc = next_pc;
	/* Size bounds alone cannot establish that RAM scripts leave this tail
	 * unused as data. Require separately established ownership. */
	if (!dreem_ram_tail_confirmed)
		ret = -EOPNOTSUPP;
done:
	sdma->eeg.error = ret;
	sdma->eeg.firmware_done = true;
	mutex_unlock(&sdma->eeg.lock);
	if (fw)
		release_firmware(fw);
}

static ssize_t dreem_user_script_store(struct device *dev,
		struct device_attribute *attr, const char *buffer, size_t count)
{
	struct sdma_engine *sdma = dev_get_drvdata(dev);
	struct dreem_sdma *eeg = &sdma->eeg;
	int ret;
	if (!count || (count & 1) || count > DREEM_SCRIPT_BYTES)
		return -EINVAL;
	mutex_lock(&eeg->lock);
	ret = eeg->error;
	if (ret)
		goto out;
	if (eeg->removed || eeg->armed) {
		ret = -EBUSY;
		goto out;
	}
	if (!eeg->firmware_done) {
		ret = -EAGAIN;
		goto out;
	}
	if (eeg->pc < DREEM_PROGRAM_START || eeg->pc >= DREEM_PROGRAM_END ||
	    count / 2 > DREEM_PROGRAM_END - eeg->pc) {
		ret = -ENOSPC;
		goto out;
	}
	ret = dreem_sdma_clocks_get(sdma);
	if (ret)
		goto out;
	ret = sdma_load_script(sdma, (void *)buffer, count, eeg->pc);
	/* Keep clocks and DMA storage on a command timeout until reboot. */
	if (eeg->command_failed)
		eeg->clocks_held = true;
	else
		dreem_sdma_clocks_put(sdma);
	eeg->script_loaded = !ret;
	eeg->managed = false;
out:
	mutex_unlock(&eeg->lock);
	return ret ? ret : count;
}

/* eeg.lock serializes the consumer and loader. progress_lock covers IRQ
 * state transitions; IRQ never acquires eeg.lock or channel_0_lock. */
static int dreem_sdma_pause_locked(struct sdma_engine *sdma)
{
	struct dreem_sdma *eeg = &sdma->eeg;
	unsigned long flags;
	unsigned int attempt;
	int ret;
	if (eeg->paused)
		return 0;
	if (eeg->error)
		return eeg->error;
	if (eeg->request >= 0xfffffffeu)
		return -EOVERFLOW;
	spin_lock_irqsave(&eeg->progress_lock, flags);
	eeg->pausing = true;
	dreem_sdma_event(sdma, false);
	eeg->request = (eeg->request + 2) & ~1u;
	WRITE_ONCE(eeg->counter[1], eeg->request);
	dma_wmb();
	writel(BIT(DREEM_CHANNEL), sdma->regs + SDMA_H_INTR);
	sdma_set_channel_priority(&sdma->channel[DREEM_CHANNEL], 1);
	writel(BIT(DREEM_CHANNEL), sdma->regs + SDMA_H_EVTPEND);
	sdma_enable_channel(sdma, DREEM_CHANNEL);
	spin_unlock_irqrestore(&eeg->progress_lock, flags);
	for (attempt = 0; attempt < 1000; ++attempt) {
		u32 ack = READ_ONCE(eeg->counter[2]);
		dma_rmb();
		ret = READ_ONCE(eeg->error);
		if (!ret && READ_ONCE(eeg->counter[3]))
			ret = -EIO;
		if (ret)
			break;
		if (ack == eeg->request &&
		    !(readl(sdma->regs + SDMA_H_EVTPEND) & BIT(DREEM_CHANNEL))) {
			/* Drain pending IRQ accounting before the consumer resets its ring.
			 * A late/coalesced interrupt must not suppress the first new frame. */
			synchronize_irq(eeg->irq);
			spin_lock_irqsave(&eeg->progress_lock, flags);
			eeg->paused = true;
			eeg->pausing = false;
			dreem_sdma_stop(sdma);
			ret = sdma_eeg_advance(&eeg->progress, READ_ONCE(eeg->counter[0]),
					       dreem_sdma_notify, sdma);
			if (ret >= 0)
				ret = READ_ONCE(eeg->error);
			writel(BIT(DREEM_CHANNEL), sdma->regs + SDMA_H_INTR);
			spin_unlock_irqrestore(&eeg->progress_lock, flags);
			if (!ret)
				return 0;
			break;
		}
		usleep_range(100, 200);
	}
	if (attempt == 1000)
		ret = -ETIMEDOUT;
	spin_lock_irqsave(&eeg->progress_lock, flags);
	eeg->pausing = false;
	WRITE_ONCE(eeg->error, ret);
	WRITE_ONCE(dreem_sdma_error, ret);
	dreem_sdma_stop(sdma);
	up(&ads_data_sem);
	spin_unlock_irqrestore(&eeg->progress_lock, flags);
	return ret;
}

int dreem_sdma_control(unsigned int command)
{
	struct sdma_engine *sdma = READ_ONCE(dreem_sdma_owner);
	struct dreem_sdma *eeg;
	unsigned long flags;
	int ret = 0;
	if (!sdma)
		return -ENODEV;
	eeg = &sdma->eeg;
	mutex_lock(&eeg->lock);
	if (!eeg->managed || !eeg->published || eeg->removed) {
		ret = -EOPNOTSUPP;
		goto out;
	}
	if (command == DREEM_SDMA_CLAIM) {
		if (eeg->claimed)
			ret = -EBUSY;
		else if (eeg->error)
			ret = eeg->error;
		else if (!(ret = dreem_sdma_pause_locked(sdma)))
			eeg->claimed = true;
	} else if (!eeg->claimed) {
		ret = -EPERM;
	} else if (command == DREEM_SDMA_PAUSE || command == DREEM_SDMA_RELEASE) {
		ret = dreem_sdma_pause_locked(sdma);
		if (!ret && command == DREEM_SDMA_RELEASE)
			eeg->claimed = false;
	} else if (command == DREEM_SDMA_RUN) {
		if (eeg->error)
			ret = eeg->error;
		else if (!eeg->paused)
			ret = -EBUSY;
		else if (eeg->request >= 0xfffffffeu)
			ret = -EOVERFLOW;
		else {
			spin_lock_irqsave(&eeg->progress_lock, flags);
			ret = READ_ONCE(eeg->error);
			if (!ret) {
				writel(BIT(DREEM_CHANNEL), sdma->regs + SDMA_H_INTR);
				WRITE_ONCE(eeg->counter[1], ++eeg->request);
				dma_wmb();
				eeg->paused = false;
				sdma_set_channel_priority(&sdma->channel[DREEM_CHANNEL], 1);
				writel(BIT(DREEM_CHANNEL), sdma->regs + SDMA_H_EVTPEND);
				sdma_enable_channel(sdma, DREEM_CHANNEL);
				dreem_sdma_event(sdma, true);
			}
			spin_unlock_irqrestore(&eeg->progress_lock, flags);
		}
	} else {
		ret = -EINVAL;
	}
out:
	mutex_unlock(&eeg->lock);
	return ret;
}
EXPORT_SYMBOL_GPL(dreem_sdma_control);

static int dreem_sdma_start(struct sdma_engine *sdma)
{
	struct dreem_sdma *eeg = &sdma->eeg;
	struct sdma_buffer_descriptor *bd = sdma->bd0;
	struct sdma_eeg_context_input input;
	unsigned long flags;
	unsigned int attempt;
	size_t control_bytes = eeg->managed ? DREEM_CONTROL_BYTES : sizeof(*eeg->counter);
	int ret;
	if (eeg->error)
		return eeg->error;
	if (eeg->removed)
		return -ENODEV;
	if (eeg->armed)
		return -EBUSY; /* Do not inject an extra event into a running script. */
	if (!eeg->script_loaded)
		return -ENODATA;
	eeg->ring = dma_alloc_coherent(sdma->dev, SDMA_EEG_RING_BYTES,
				      &eeg->ring_phys, GFP_KERNEL);
	if (!eeg->ring)
		return -ENOMEM;
	eeg->counter = dma_alloc_coherent(sdma->dev, control_bytes,
					 &eeg->counter_phys, GFP_KERNEL);
	if (!eeg->counter) {
		ret = -ENOMEM;
		goto free_ring;
	}
	memset(eeg->ring, 0, SDMA_EEG_RING_BYTES);
	memset(eeg->counter, 0, control_bytes);
	input.pc = eeg->pc;
	input.ring_phys = eeg->ring_phys;
	input.counter_phys = eeg->counter_phys;
	memcpy(input.registers, eeg->registers, sizeof(input.registers));
	if (eeg->managed) {
		if ((eeg->counter_phys & 63) || eeg->counter_phys > 0xffffffc0u ||
		    (eeg->ring_phys < eeg->counter_phys + control_bytes &&
		     eeg->counter_phys < eeg->ring_phys + SDMA_EEG_RING_BYTES)) {
			ret = -EINVAL;
			goto free_counter;
		}
		input.registers[3] = 0x02008000;
		eeg->progress.initialized = 1;
		eeg->pausing = true;
	}
	ret = dreem_sdma_clocks_get(sdma);
	if (ret)
		goto free_counter;
	spin_lock_irqsave(&sdma->channel_0_lock, flags);
	ret = sdma_eeg_prepare_context(&input, (u32 *)sdma->context,
				       sizeof(*sdma->context) / sizeof(u32));
	if (ret) {
		spin_unlock_irqrestore(&sdma->channel_0_lock, flags);
		dreem_sdma_clocks_put(sdma);
		goto free_counter;
	}
	/* From this point, DMA may have observed these buffers. They remain pinned
	 * even on timeout; clearing HE does not prove DMA has finished. */
	get_device(sdma->dev);
	eeg->clocks_held = true;
	bd->mode.command = C0_SETDM;
	bd->mode.status = BD_DONE | BD_INTR | BD_WRAP | BD_EXTD;
	bd->mode.count = SDMA_EEG_CONTEXT_WORDS;
	bd->buffer_addr = sdma->context_phys;
	bd->ext_buffer_addr = 2048 + SDMA_EEG_CONTEXT_WORDS * DREEM_CHANNEL;
	dma_wmb();
	ret = sdma_run_channel0(sdma);
	spin_unlock_irqrestore(&sdma->channel_0_lock, flags);
	if (ret)
		goto failed;
	dreem_sdma_stop(sdma);
	sdma_config_ownership(&sdma->channel[DREEM_CHANNEL], true, false, false);
	sdma->channel_control[DREEM_CHANNEL].base_bd_ptr = 0;
	sdma->channel_control[DREEM_CHANNEL].current_bd_ptr = 0;
	sdma->channel[DREEM_CHANNEL].context_loaded = true;
	writel(BIT(DREEM_CHANNEL), sdma->regs + SDMA_H_INTR);
	WRITE_ONCE(eeg->armed, true);
	if (eeg->managed) {
		ret = dreem_sdma_pause_locked(sdma);
		if (ret)
			goto failed;
		goto publish;
	}
	dma_wmb();
	sdma_set_channel_priority(&sdma->channel[DREEM_CHANNEL], 1);
	/* The original boot script sets this initialization event explicitly. */
	writel(BIT(DREEM_CHANNEL), sdma->regs + SDMA_H_EVTPEND);
	sdma_enable_channel(sdma, DREEM_CHANNEL);
	/* The recovered script initializes r3-r6, then executes done 4, which
	 * clears EP without raising HI. Waiting for an IRQ here would deadlock
	 * startup until the ADC produces its first sample. */
	for (attempt = 0; attempt < 1000; ++attempt) {
		if (!(readl(sdma->regs + SDMA_H_EVTPEND) & BIT(DREEM_CHANNEL)))
			break;
		usleep_range(100, 200);
	}
	if (attempt == 1000) {
		ret = -ETIMEDOUT;
		goto failed;
	}
publish:
	/* Serialize publication with the IRQ fault path, so a concurrent fault
	 * cannot be overwritten with a successful global status. */
	spin_lock_irqsave(&eeg->progress_lock, flags);
	ret = READ_ONCE(eeg->error);
	if (!ret) {
		smp_wmb();
		WRITE_ONCE(sdma_ads_user_buffer, eeg->ring);
		WRITE_ONCE(dreem_sdma_error, 0);
		eeg->published = true;
	}
	spin_unlock_irqrestore(&eeg->progress_lock, flags);
	if (ret)
		goto failed;
	return 0;
failed:
	WRITE_ONCE(eeg->error, ret);
	WRITE_ONCE(dreem_sdma_error, ret);
	dreem_sdma_stop(sdma);
	synchronize_irq(eeg->irq);
	return ret;
free_counter:
	dma_free_coherent(sdma->dev, control_bytes, eeg->counter, eeg->counter_phys);
	eeg->counter = NULL;
free_ring:
	dma_free_coherent(sdma->dev, SDMA_EEG_RING_BYTES, eeg->ring, eeg->ring_phys);
	eeg->ring = NULL;
	return ret;
}

static ssize_t dreem_trigger_store(struct device *dev,
		struct device_attribute *attr, const char *buffer, size_t count)
{
	struct sdma_engine *sdma = dev_get_drvdata(dev);
	unsigned long value;
	int ret = kstrtoul(buffer, 0, &value);
	if (ret || (value != 1 && value != 2))
		return -EINVAL;
	mutex_lock(&sdma->eeg.lock);
	if (value == 2) {
		struct dreem_sdma *eeg = &sdma->eeg;
		ret = eeg->error;
		if (!ret && (eeg->armed || eeg->removed))
			ret = -EBUSY;
		if (!ret && !eeg->firmware_done)
			ret = -EAGAIN;
		if (!ret && (eeg->pc < DREEM_PROGRAM_START || eeg->pc >= DREEM_PROGRAM_END ||
		    ARRAY_SIZE(dreem_acquire_code) > DREEM_PROGRAM_END - eeg->pc))
			ret = -ENOSPC;
		if (ret)
			goto out;
		ret = dreem_sdma_clocks_get(sdma);
		if (ret)
			goto out;
		ret = sdma_load_script(sdma, (void *)dreem_acquire_code,
				       sizeof(dreem_acquire_code), eeg->pc);
		if (eeg->command_failed)
			eeg->clocks_held = true;
		else
			dreem_sdma_clocks_put(sdma);
		eeg->script_loaded = !ret;
		eeg->managed = !ret;
		if (ret)
			goto out;
	}
	ret = dreem_sdma_start(sdma);
out:
	mutex_unlock(&sdma->eeg.lock);
	return ret ? ret : count;
}

static ssize_t dreem_status_show(struct device *dev,
		struct device_attribute *attr, char *buffer)
{
	return scnprintf(buffer, PAGE_SIZE, "%d\n", dreem_sdma_status());
}

struct dreem_reg_attr {
	struct device_attribute attr;
	unsigned int index;
};

static ssize_t dreem_reg_show(struct device *dev,
		struct device_attribute *attr, char *buffer)
{
	struct sdma_engine *sdma = dev_get_drvdata(dev);
	struct dreem_reg_attr *reg = container_of(attr, struct dreem_reg_attr, attr);
	ssize_t ret;
	mutex_lock(&sdma->eeg.lock);
	ret = scnprintf(buffer, PAGE_SIZE, "%u\n", sdma->eeg.registers[reg->index]);
	mutex_unlock(&sdma->eeg.lock);
	return ret;
}

static ssize_t dreem_reg_store(struct device *dev,
		struct device_attribute *attr, const char *buffer, size_t count)
{
	struct sdma_engine *sdma = dev_get_drvdata(dev);
	struct dreem_reg_attr *reg = container_of(attr, struct dreem_reg_attr, attr);
	u32 value;
	int ret = kstrtou32(buffer, 0, &value);
	if (ret)
		return ret;
	mutex_lock(&sdma->eeg.lock);
	ret = sdma->eeg.error;
	if (!ret && (sdma->eeg.armed || sdma->eeg.removed))
		ret = -EBUSY;
	if (!ret)
		sdma->eeg.registers[reg->index] = value;
	mutex_unlock(&sdma->eeg.lock);
	return ret ? ret : count;
}

#define DREEM_REG(n) static struct dreem_reg_attr dreem_reg_##n = { \
	.attr = __ATTR(reg_r##n, 0600, dreem_reg_show, dreem_reg_store), .index = n }
DREEM_REG(0); DREEM_REG(1); DREEM_REG(2); DREEM_REG(3);
DREEM_REG(4); DREEM_REG(5); DREEM_REG(6); DREEM_REG(7);
#undef DREEM_REG

static DEVICE_ATTR(user_script, 0200, NULL, dreem_user_script_store);
static DEVICE_ATTR(trigger, 0200, NULL, dreem_trigger_store);
static DEVICE_ATTR(eeg_status, 0400, dreem_status_show, NULL);
static struct attribute *dreem_sdma_attrs[] = {
	&dev_attr_user_script.attr, &dev_attr_trigger.attr, &dev_attr_eeg_status.attr,
	&dreem_reg_0.attr.attr, &dreem_reg_1.attr.attr,
	&dreem_reg_2.attr.attr, &dreem_reg_3.attr.attr,
	&dreem_reg_4.attr.attr, &dreem_reg_5.attr.attr,
	&dreem_reg_6.attr.attr, &dreem_reg_7.attr.attr, NULL,
};
static const struct attribute_group dreem_sdma_group = { .attrs = dreem_sdma_attrs };

static int dreem_sdma_register(struct sdma_engine *sdma)
{
	int ret;
	if (!sdma->eeg.enabled)
		return 0;
	if (dreem_sdma_owner)
		return -EBUSY;
	mutex_lock(&sdma->eeg.lock);
	ret = sysfs_create_group(&sdma->dev->kobj, &dreem_sdma_group);
	if (!ret) {
		dreem_sdma_owner = sdma;
		WRITE_ONCE(dreem_sdma_error, sdma->eeg.error ?: -EAGAIN);
	}
	mutex_unlock(&sdma->eeg.lock);
	return ret;
}

static void dreem_sdma_remove(struct sdma_engine *sdma)
{
	if (!sdma->eeg.enabled)
		return;
	sysfs_remove_group(&sdma->dev->kobj, &dreem_sdma_group);
	mutex_lock(&sdma->eeg.lock);
	sdma->eeg.removed = true;
	WRITE_ONCE(sdma->eeg.error, -ENODEV);
	WRITE_ONCE(dreem_sdma_error, -ENODEV);
	if (sdma->eeg.clocks_held) {
		dreem_sdma_stop(sdma);
		synchronize_irq(sdma->eeg.irq);
	}
	mutex_unlock(&sdma->eeg.lock);
	/* No freeing or republishing DMA storage. Reprobe requires reboot. */
}
