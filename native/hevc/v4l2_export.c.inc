/* SPDX-License-Identifier: LGPL-2.1-or-later */
/* Explicit interface; caller holds the AVFrame until GPU work
 * completes, so the driver's capture buffer cannot be requeued meanwhile. */
int frameport_v4l2_export_frame(const AVFrame *frame);
int frameport_v4l2_export_frame(const AVFrame *frame)
{
    V4L2Buffer *buf;
    struct v4l2_exportbuffer exp = {0};
    if (!frame || frame->format != AV_PIX_FMT_NV12 || !frame->buf[0])
        return AVERROR(EINVAL);
    buf = av_buffer_get_opaque(frame->buf[0]);
    if (!buf || buf->num_planes != 1 || buf->context->type != V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE ||
        buf->plane_info[0].mm_addr != frame->buf[0]->data || frame->data[0] != frame->buf[0]->data)
        return AVERROR(EINVAL);
    exp.type = buf->buf.type;
    exp.index = buf->buf.index;
    exp.flags = O_CLOEXEC | O_RDWR;
    if (ioctl(buf_to_m2mctx(buf)->fd, VIDIOC_EXPBUF, &exp) < 0)
        return AVERROR(errno);
    return exp.fd;
}
