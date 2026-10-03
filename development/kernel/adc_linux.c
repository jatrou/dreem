// SPDX-License-Identifier: GPL-2.0-only
/* Research adapter for the reviewed stock Dreem SDMA exports. Not qualified
 * for installation. See kernel-integration.md for build and runtime gates. */
#include <linux/atomic.h>
#include <linux/delay.h>
#include <linux/fs.h>
#include <linux/gpio.h>
#include <linux/io.h>
#include <linux/jiffies.h>
#include <linux/kref.h>
#include <linux/miscdevice.h>
#include <linux/module.h>
#include <linux/mutex.h>
#include <linux/of.h>
#include <linux/of_address.h>
#include <linux/pm_runtime.h>
#include <linux/sched.h>
#include <linux/semaphore.h>
#include <linux/slab.h>
#include <linux/spi/spi.h>
#include <linux/uaccess.h>

#include "ads129x_init.h"

/* These objects are supplied by the reviewed vendor kernel, not by NXP's
 * unmodified baseline. The builder checks all imported symbol CRCs. */
extern u8 *sdma_ads_user_buffer;
extern int sdma_queue_head;
extern struct semaphore ads_data_sem;

#ifdef CONFIG_DREEM_EEG_SDMA
extern int dreem_sdma_status(void);
#else
static int dreem_sdma_status(void) { return 0; }
#endif

static bool sdma_hardware_confirmed;
module_param(sdma_hardware_confirmed, bool, 0400);
MODULE_PARM_DESC(sdma_hardware_confirmed,
	"Allow binding only after the nonzero hardware revision and SDMA setup are verified");

#define SPI_PHYS 0x02008000u
#define CLOCK_PHYS 0x020c406cu
#define EVENT_PHYS 0x020ec20cu

struct dreem_adc {
	struct spi_device *spi;
	struct miscdevice misc;
	struct kref ref;
	struct mutex lock;
	atomic_t detached, cancelled;
	void __iomem *spi_regs, *clock_reg, *event_reg;
	struct ads_transport transport;
	struct ads_sdma_state state;
	bool opened, initialized, running, runtime_held;
	bool gpio_power, gpio_cs, gpio_drdy, nonblock, pending;
	int io_error;
	u8 pending_record[16];
};

static void __iomem *adc_register(struct dreem_adc *adc, u32 address)
{
	if (address >= SPI_PHYS && address <= SPI_PHYS + 24 && !(address & 3))
		return adc->spi_regs + address - SPI_PHYS;
	if (address == CLOCK_PHYS)
		return adc->clock_reg;
	if (address == EVENT_PHYS)
		return adc->event_reg;
	adc->io_error = -EINVAL;
	return NULL;
}

static int adc_wait(struct dreem_adc *adc)
{
	int attempt;
	if (adc->io_error)
		return adc->io_error;
	for (attempt = 0; attempt < 10; ++attempt) {
		int provider_error = dreem_sdma_status();
		if (provider_error)
			return adc->io_error = provider_error;
		if (atomic_read(&adc->detached) || atomic_read(&adc->cancelled))
			return adc->io_error = -ESHUTDOWN;
		if (signal_pending(current))
			return adc->io_error = -EINTR;
		if (adc->nonblock) {
			if (down_trylock(&ads_data_sem))
				return adc->io_error = -EAGAIN;
			return adc->io_error = dreem_sdma_status();
		}
		if (!down_timeout(&ads_data_sem, msecs_to_jiffies(100)))
			return adc->io_error = dreem_sdma_status();
	}
	return adc->io_error = -ETIMEDOUT;
}

static u32 adc_io(void *context, enum ads_io_operation operation, u32 a, u32 b)
{
	struct dreem_adc *adc = context;
	void __iomem *reg;
	int ret;
	switch (operation) {
	case ADS_READ32:
		reg = adc_register(adc, a);
		return reg ? readl(reg) : 0;
	case ADS_WRITE32:
		reg = adc_register(adc, a);
		if (a == EVENT_PHYS && b == 2)
			dma_wmb();
		if (reg)
			writel(b, reg);
		return 0;
	case ADS_GPIO_OUTPUT:
		ret = gpio_direction_output(a, b);
		if (ret)
			adc->io_error = ret;
		return ret;
	case ADS_GPIO_SET:
		gpio_set_value(a, b);
		return 0;
	case ADS_SLEEP_MS:
		msleep(a);
		return 0;
	case ADS_SLEEP_US_RANGE:
		usleep_range(a, b);
		return 0;
	case ADS_QUEUE_HEAD:
		return READ_ONCE(sdma_queue_head);
	case ADS_QUEUE_TRYLOCK:
		return down_trylock(&ads_data_sem);
	case ADS_QUEUE_WAIT:
		ret = adc_wait(adc);
		if (!ret)
			dma_rmb();
		return ret;
	}
	adc->io_error = -EINVAL;
	return -EINVAL;
}

static void adc_free(struct kref *ref)
{
	struct dreem_adc *adc = container_of(ref, struct dreem_adc, ref);
	if (adc->gpio_drdy)
		gpio_free(34);
	if (adc->gpio_cs)
		gpio_free(90);
	if (adc->gpio_power)
		gpio_free(35);
	if (adc->event_reg)
		iounmap(adc->event_reg);
	if (adc->clock_reg)
		iounmap(adc->clock_reg);
	if (adc->spi_regs)
		iounmap(adc->spi_regs);
	spi_dev_put(adc->spi);
	kfree(adc);
}

/* Caller holds lock. Memory/resources outlive open descriptors on unbind. */
static void adc_shutdown(struct dreem_adc *adc)
{
	if (adc->initialized) {
		spi_bus_lock(adc->spi->master);
		ads129x_sdma_release(&adc->transport);
		spi_bus_unlock(adc->spi->master);
	}
	adc->initialized = adc->running = adc->pending = false;
	if (adc->runtime_held) {
		pm_runtime_mark_last_busy(adc->spi->master->dev.parent);
		pm_runtime_put_autosuspend(adc->spi->master->dev.parent);
		adc->runtime_held = false;
	}
}

static int adc_open(struct inode *inode, struct file *file)
{
	struct miscdevice *misc = file->private_data;
	struct dreem_adc *adc = container_of(misc, struct dreem_adc, misc);
	int ret;
	if (mutex_lock_interruptible(&adc->lock))
		return -ERESTARTSYS;
	if (atomic_read(&adc->detached)) {
		ret = -ENODEV;
		goto out;
	}
	if (adc->opened) {
		ret = -EBUSY;
		goto out;
	}
	ret = dreem_sdma_status();
	if (ret)
		goto out;
	adc->state.ring = (u8 *)READ_ONCE(sdma_ads_user_buffer);
	if (!adc->state.ring) {
		ret = -EAGAIN;
		goto out;
	}
	ret = pm_runtime_get_sync(adc->spi->master->dev.parent);
	if (ret < 0) {
		pm_runtime_put_noidle(adc->spi->master->dev.parent);
		goto out;
	}
	adc->runtime_held = true;
	ret = spi_bus_lock(adc->spi->master);
	if (ret)
		goto failed;
	adc->io_error = 0;
	ret = ads129x_sdma_initialize(&adc->transport);
	spi_bus_unlock(adc->spi->master);
	if (ret || adc->io_error) {
		ret = adc->io_error ? adc->io_error : ret;
		goto failed;
	}
	adc->opened = adc->initialized = true;
	atomic_set(&adc->cancelled, 0);
	kref_get(&adc->ref);
	file->private_data = adc;
	ret = 0;
	goto out;
failed:
	adc_shutdown(adc);
out:
	mutex_unlock(&adc->lock);
	return ret;
}

static int adc_close(struct inode *inode, struct file *file)
{
	struct dreem_adc *adc = file->private_data;
	atomic_set(&adc->cancelled, 1);
	mutex_lock(&adc->lock);
	adc_shutdown(adc);
	adc->opened = false;
	mutex_unlock(&adc->lock);
	kref_put(&adc->ref, adc_free);
	return 0;
}

static ssize_t adc_read(struct file *file, char __user *buffer, size_t size, loff_t *position)
{
	struct dreem_adc *adc = file->private_data;
	int ret;
	if (size < 16)
		return -EINVAL;
	if (mutex_lock_interruptible(&adc->lock))
		return -ERESTARTSYS;
	if (atomic_read(&adc->detached)) {
		ret = -ENODEV;
		goto out;
	}
	if (!adc->running || atomic_read(&adc->cancelled)) {
		ret = -EPIPE;
		goto out;
	}
	ret = dreem_sdma_status();
	if (ret) {
		adc_shutdown(adc);
		goto out;
	}
	if (!adc->pending) {
		adc->io_error = 0;
		adc->nonblock = file->f_flags & O_NONBLOCK;
		ret = ads129x_sdma_read_frame(&adc->transport, &adc->state,
					    adc->pending_record, sizeof(adc->pending_record));
		if (ret != 16) {
			if (adc->io_error)
				ret = adc->io_error;
			if (dreem_sdma_status())
				adc_shutdown(adc);
			goto out;
		}
		adc->pending = true;
	}
	ret = dreem_sdma_status();
	if (ret) {
		adc_shutdown(adc);
		goto out;
	}
	if (copy_to_user(buffer, adc->pending_record, 16)) {
		ret = -EFAULT; /* Retain this frame for the next successful copy. */
		goto out;
	}
	adc->pending = false;
	ret = 16;
out:
	mutex_unlock(&adc->lock);
	return ret;
}

static long adc_ioctl(struct file *file, unsigned int command, unsigned long argument)
{
	struct dreem_adc *adc = file->private_data;
	int ret;
	if (command != 0 && command != 1 && command != 4 && command != 5)
		return -ENOTTY;
	if (command == 0)
		atomic_set(&adc->cancelled, 1);
	if (command == 0)
		mutex_lock(&adc->lock);
	else if (mutex_lock_interruptible(&adc->lock))
		return -ERESTARTSYS;
	if (atomic_read(&adc->detached)) {
		ret = -ENODEV;
		goto out;
	}
	if (!adc->initialized) {
		ret = -EIO;
		goto out;
	}
	if ((command == 1 || command == 5) && adc->running) {
		ret = -EBUSY;
		goto out;
	}
	if (command == 1 || command == 5) {
		ret = dreem_sdma_status();
		if (ret) {
			adc_shutdown(adc);
			goto out;
		}
	}
	adc->io_error = 0;
	if (command != 4) {
		ret = spi_bus_lock(adc->spi->master);
		if (ret)
			goto out;
	}
	switch (command) {
	case 0:
		ret = ads129x_sdma_stop(&adc->transport);
		adc->running = adc->pending = false;
		break;
	case 1:
		adc->pending = false;
		/* Ring reset must be visible before enabling peripheral requests. */
		ret = ads129x_sdma_start(&adc->transport, &adc->state);
		if (!ret) {
			adc->running = true;
			atomic_set(&adc->cancelled, 0);
		}
		break;
	case 5:
		ret = ads129x_sdma_test_signal(&adc->transport);
		adc->pending = false;
		break;
	default:
		ret = copy_to_user((void __user *)argument, &adc->state.errors,
				   sizeof(adc->state.errors)) ? -EFAULT : 0;
		break;
	}
	if (command != 4)
		spi_bus_unlock(adc->spi->master);
	if (adc->io_error)
		ret = adc->io_error;
	if (ret && command != 4) {
		adc->initialized = adc->running = false;
		atomic_set(&adc->cancelled, 1);
	}
out:
	mutex_unlock(&adc->lock);
	return ret;
}

static const struct file_operations adc_fops = {
	.owner = THIS_MODULE,
	.open = adc_open,
	.release = adc_close,
	.read = adc_read,
	.unlocked_ioctl = adc_ioctl,
	.llseek = no_llseek,
};

static int adc_probe(struct spi_device *spi)
{
	struct dreem_adc *adc;
	struct resource resource;
	int ret;
	if (!sdma_hardware_confirmed || !of_machine_is_compatible("fsl,imx6ull-femto"))
		return -ENODEV;
	/* Restrict raw-SPI ownership to the sole device on a one-CS controller.
	 * A mutex must not be held across open/close, which may run in different
	 * tasks. Lock individual control transactions in their calling task. */
	if (spi->chip_select || spi->master->num_chipselect != 1 ||
	    of_address_to_resource(spi->master->dev.parent->of_node, 0, &resource) ||
	    resource.start != SPI_PHYS)
		return -ENODEV;
	if (!READ_ONCE(sdma_ads_user_buffer))
		return -EPROBE_DEFER;
	adc = kzalloc(sizeof(*adc), GFP_KERNEL);
	if (!adc)
		return -ENOMEM;
	kref_init(&adc->ref);
	mutex_init(&adc->lock);
	atomic_set(&adc->detached, 0);
	atomic_set(&adc->cancelled, 1);
	adc->spi = spi_dev_get(spi);
	adc->transport = (struct ads_transport){adc_io, adc, 4096};
	ret = gpio_request(35, "dreem-eeg-power");
	if (ret)
		goto failed;
	adc->gpio_power = true;
	ret = gpio_request(90, "dreem-eeg-cs");
	if (ret)
		goto failed;
	adc->gpio_cs = true;
	ret = gpio_request(34, "dreem-eeg-drdy");
	if (ret)
		goto failed;
	adc->gpio_drdy = true;
	adc->spi_regs = ioremap(SPI_PHYS, 32);
	adc->clock_reg = ioremap(CLOCK_PHYS, 4);
	adc->event_reg = ioremap(EVENT_PHYS, 4);
	if (!adc->spi_regs || !adc->clock_reg || !adc->event_reg) {
		ret = -ENOMEM;
		goto failed;
	}
	adc->misc.minor = MISC_DYNAMIC_MINOR;
	adc->misc.name = "eeg_cdev";
	adc->misc.fops = &adc_fops;
	adc->misc.parent = &spi->dev;
	adc->misc.mode = 0600;
	ret = misc_register(&adc->misc);
	if (ret)
		goto failed;
	spi_set_drvdata(spi, adc);
	return 0;
failed:
	kref_put(&adc->ref, adc_free);
	return ret;
}

static int adc_remove(struct spi_device *spi)
{
	struct dreem_adc *adc = spi_get_drvdata(spi);
	atomic_set(&adc->detached, 1);
	atomic_set(&adc->cancelled, 1);
	misc_deregister(&adc->misc);
	mutex_lock(&adc->lock);
	adc_shutdown(adc);
	mutex_unlock(&adc->lock);
	spi_set_drvdata(spi, NULL);
	kref_put(&adc->ref, adc_free);
	return 0;
}

static int adc_suspend(struct device *device)
{
	struct dreem_adc *adc = spi_get_drvdata(to_spi_device(device));
	int ret;
	mutex_lock(&adc->lock);
	ret = adc->opened ? -EBUSY : 0;
	mutex_unlock(&adc->lock);
	return ret;
}

static SIMPLE_DEV_PM_OPS(adc_pm, adc_suspend, NULL);
static const struct of_device_id adc_of_match[] = {
	{ .compatible = "eeg" }, { }
};
MODULE_DEVICE_TABLE(of, adc_of_match);
static const struct spi_device_id adc_ids[] = {
	{ "eeg", 0 }, { }
};
MODULE_DEVICE_TABLE(spi, adc_ids);
static struct spi_driver adc_driver = {
	.driver = {
		.name = "dreem_eeg_research",
		.of_match_table = adc_of_match,
		.pm = &adc_pm,
	},
	.probe = adc_probe,
	.remove = adc_remove,
	.id_table = adc_ids,
};
module_spi_driver(adc_driver);
MODULE_LICENSE("GPL v2");
MODULE_DESCRIPTION("Reconstructed Dreem EEG reader using stock SDMA exports; research only");
