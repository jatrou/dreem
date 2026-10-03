/* SPDX-License-Identifier: GPL-2.0-or-later */
/* New lifetime protection for the reconstructed Femto jack interface. */
static DEFINE_MUTEX(dreem_jack_mutex);
static struct platform_device *dreem_audio_owner;
static unsigned long dreem_jack_generation;
static bool dreem_jack_ready;

static int jack_open(struct inode *inode, struct file *file)
{
	int ret = -ENODEV;

	mutex_lock(&dreem_jack_mutex);
	if (dreem_jack_ready) {
		/* This is an epoch, never a dereferenced pointer or device address. */
		file->private_data = (void *)dreem_jack_generation;
		ret = nonseekable_open(inode, file);
	}
	mutex_unlock(&dreem_jack_mutex);
	return ret;
}

static long jack_ioctl(struct file *file, unsigned int command,
		       unsigned long argument)
{
	int ret = -ENODEV;

	mutex_lock(&dreem_jack_mutex);
	if (!dreem_jack_ready ||
	    (unsigned long)file->private_data != dreem_jack_generation)
		goto out;
	switch (command) {
	case 5:
		set_jack_status(1);
		ret = 0;
		break;
	case 6:
		set_jack_status(0);
		ret = 0;
		break;
	default:
		ret = -ENOTTY;
		break;
	}
out:
	mutex_unlock(&dreem_jack_mutex);
	return ret;
}

static const struct file_operations fops = {
	.owner = THIS_MODULE,
	.open = jack_open,
	.unlocked_ioctl = jack_ioctl,
	.llseek = no_llseek,
};

static struct miscdevice dreem_jack_device = {
	.minor = MISC_DYNAMIC_MINOR,
	.name = "jack",
	.fops = &fops,
	.mode = 0600,
};
