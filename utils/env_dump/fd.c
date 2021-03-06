/* Copyright (c) 2015, Intel Corporation
 *
 * Redistribution and use in source and binary forms, with or without
 * modification, are permitted provided that the following conditions are met:
 *
 *     * Redistributions of source code must retain the above copyright notice,
 *       this list of conditions and the following disclaimer.
 *     * Redistributions in binary form must reproduce the above copyright
 *       notice, this list of conditions and the following disclaimer in the
 *       documentation and/or other materials provided with the distribution.
 *     * Neither the name of Intel Corporation nor the names of its contributors
 *       may be used to endorse or promote products derived from this software
 *       without specific prior written permission.
 *
 * THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
 * AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
 * IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
 * DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT OWNER OR CONTRIBUTORS BE LIABLE
 * FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
 * DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
 * SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
 * CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
 * OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
 * OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
 */

#define _GNU_SOURCE

#include "env_dump.h"

#include <sys/types.h>
#include <pthread.h>
#include <unistd.h>
#include <stdarg.h>
#include <stdlib.h>
#include <dlfcn.h>
#include <link.h>

/* Have a small DB of booleans that will store if we already accounted the
 * fd (1) or not yet (0). This DB can hold up to 2048, which is twice the usual
 * limit found on Linux.
 */
static pthread_mutex_t fd_mp = PTHREAD_MUTEX_INITIALIZER;
static unsigned long fds[64] = { 0 };

/* get the possibility to call hooks when closing an fd */
static struct fd_close_cb_t {
	fd_callback cb;
	int fd;
	void *user;
} *fd_close_cb;
static size_t fd_close_cb_len;

static inline int
bit_read(int bit)
{
	int val;

	/* If we go out of the expected range, just give up and say it has been
	 * reported already!
	 */
	if (unlikely(bit > sizeof(fds) * 8))
		return 1;

	val = (fds[bit >> 5] >> (bit & 0x1f)) & 1;

	return val;
}

static inline void
bit_write(int bit, int value)
{
	int idxh, idxl;

	if (unlikely(bit > sizeof(fds) * 8))
		return;

	idxh = bit >> 5;
	idxl = bit & 0x1f;
	fds[idxh] = (fds[idxh] & ~(1 << idxl)) | (value << idxl);
}

static ssize_t
get_path_from_fd(int fd, char *buf, size_t bufsiz)
{
	/* longest fd path is /proc/4194303/fd/1024 --> 21 chars */
	char proc_path[22];
	sprintf(proc_path, "/proc/%u/fd/%u", getpid(), fd);

	return readlink(proc_path, buf, bufsiz);
}

int
ioctl(int fd, unsigned long int request, ...)
{
	int (*orig_ioctl)(int, unsigned long int, ...);
	void *arg;
	va_list ap;

	va_start(ap, request);
	arg = va_arg(ap, void *);

	orig_ioctl = _env_dump_resolve_symbol_by_id(SYMB_IOCTL);
	if (!_env_ignored) {
		/* If it is the first time we see an ioctl on this fd */
		pthread_mutex_lock(&fd_mp);

		if (!bit_read(fd)) {
			char path[101];
			size_t len = get_path_from_fd(fd, path, sizeof(path));
			path[len] = '\0';
			if (path[0] == '/')
				fprintf(env_file, "IOCTL,%s\n", path);
			bit_write(fd, 1);

			pthread_mutex_unlock(&fd_mp);

			_env_dump_drm_dump_info(path, fd);
		} else
			pthread_mutex_unlock(&fd_mp);
	}

	return orig_ioctl(fd, request, arg);
}

void
_env_dump_close_callback(int fd, fd_callback cb, void *user)
{
	int idx = -1, i;

	pthread_mutex_lock(&fd_mp);
	for (i = 0; i < fd_close_cb_len; i++) {
		/* re-use a slot if available */
		if (fd_close_cb[i].cb == NULL)
			idx = i;
	}

	if (idx < 0) {
		fd_close_cb = realloc(fd_close_cb, sizeof(struct fd_close_cb_t) * (fd_close_cb_len + 1));
		/* XXX: just crash if we do not have enough RAM ... */
		idx = fd_close_cb_len;
		fd_close_cb_len++;
	}

	fd_close_cb[idx].fd = fd;
	fd_close_cb[idx].cb = cb;
	fd_close_cb[idx].user = user;

	pthread_mutex_unlock(&fd_mp);
}

static void
call_fd_close_callbacks(int fd)
{
	int i;

	pthread_mutex_lock(&fd_mp);

	/* call the callbacks */
	for (i = 0; i < fd_close_cb_len; i++) {
		/* re-use a slot if available */
		if (fd < 0 || fd_close_cb[i].fd == fd) {
			fd_callback cb = fd_close_cb[i].cb;
			int cb_fd = fd_close_cb[i].fd;
			void *cb_user = fd_close_cb[i].user;
			fd_close_cb[i].fd = -1;
			fd_close_cb[i].cb = NULL;
			fd_close_cb[i].user = NULL;
			pthread_mutex_unlock(&fd_mp);
			cb(cb_fd, cb_user);
			pthread_mutex_lock(&fd_mp);
		}
	}

	pthread_mutex_unlock(&fd_mp);
}

int
close(int fd)
{
	int (*orig_close)(int);

	orig_close = _env_dump_resolve_symbol_by_name("close");

	if (!_env_ignored) {
		/* call the callbacks */
		call_fd_close_callbacks(fd);

		pthread_mutex_lock(&fd_mp);
		bit_write(fd, 0);
		pthread_mutex_unlock(&fd_mp);
	}

	return orig_close(fd);
}

void
_env_dump_fd_init()
{

}

void
_env_dump_fd_fini()
{
	/* call the callbacks */
	call_fd_close_callbacks(-1);
}
