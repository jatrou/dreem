/* SPDX-License-Identifier: GPL-2.0-only */
/* Reconstructed built-in Femto DDR control interface. No vendor source.
 * Included with the experimental bus-frequency policy, which owns its mutex
 * and clock state. See busfreq-findings.md before any device trial. */
#include <linux/cdev.h>
#include <linux/fs.h>
#include <linux/kernel.h>

static dev_t dreem_ddr_number;
static struct cdev dreem_ddr_cdev;
static struct class *dreem_ddr_class;

static long dreem_ddr_ioctl(struct file *file, unsigned int command,
			    unsigned long argument)
{
	int ret = 0;
	/* Stock accepts unknown commands without changing the hardware. No user
	 * pointer is dereferenced; nano_core supplies only the command number. */
	if (command != 7)
		return 0;
	if (!dreem_busfreq_active())
		return -ENODEV;
	mutex_lock(&bus_freq_mutex);
	if (!bus_freq_scaling_initialized || !bus_freq_scaling_is_active)
		ret = -EAGAIN;
	else if (busfreq_suspended)
		ret = -EBUSY;
	else if (high_bus_count < 0)
		ret = -EIO;
	else if (high_bus_count == INT_MAX)
		ret = -EOVERFLOW;
	else {
		high_bus_count++;
		ret = set_high_bus_freq(1);
	}
	mutex_unlock(&bus_freq_mutex);
	return ret;
}

static const struct file_operations dreem_ddr_fops = {
	.owner = THIS_MODULE,
	.unlocked_ioctl = dreem_ddr_ioctl,
};

static int __init dreem_ddr_init(void)
{
	struct device *device;
	int ret;
	if (!dreem_busfreq_active())
		return 0;
	ret = alloc_chrdev_region(&dreem_ddr_number, 0, 1, "dreem_ddr");
	if (ret)
		return ret;
	cdev_init(&dreem_ddr_cdev, &dreem_ddr_fops);
	ret = cdev_add(&dreem_ddr_cdev, dreem_ddr_number, 1);
	if (ret) {
		kobject_put(&dreem_ddr_cdev.kobj);
		goto unregister;
	}
	dreem_ddr_class = class_create(THIS_MODULE, "dreem_ddr");
	if (IS_ERR(dreem_ddr_class)) {
		ret = PTR_ERR(dreem_ddr_class);
		goto delete;
	}
	/* Publish the node only after its file operations are registered.
	 * This is built-in, with system-lifetime ownership and no hot unload. */
	device = device_create(dreem_ddr_class, NULL, dreem_ddr_number,
			       NULL, "dreem_ddr");
	if (!IS_ERR(device))
		return 0;
	ret = PTR_ERR(device);
	class_destroy(dreem_ddr_class);
delete:
	cdev_del(&dreem_ddr_cdev);
unregister:
	unregister_chrdev_region(dreem_ddr_number, 1);
	return ret;
}
late_initcall(dreem_ddr_init);
