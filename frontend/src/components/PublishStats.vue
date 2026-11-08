<template>
  <div
    :class="['publish-stats', { 'publish-stats--compact': compact }]"
    :style="!compact ? { gridTemplateColumns: `repeat(${columns}, 1fr)` } : null"
  >
    <div
      v-for="item in metrics"
      :key="item.key"
      class="stat-item"
      :class="[
        `stat-item--${item.theme}`,
        {
          'stat-item--placeholder': isPlaceholder(item.value),
          'stat-item--compact': compact,
        },
      ]"
    >
      <el-tooltip
        content="暂无数据，可点击同步获取"
        placement="top"
        :disabled="hasData"
      >
        <!-- 数据方块：大数值在上，主题色图标底座 + 标签在下 -->
        <div v-if="!compact" class="stat-inner">
          <span class="stat-value">{{ formatValue(item.value) }}</span>
          <div class="stat-footer">
            <span class="stat-icon-chip">
              <el-icon :size="13"><component :is="item.icon" /></el-icon>
            </span>
            <span class="stat-label">{{ item.label }}</span>
          </div>
        </div>
        <!-- 紧凑模式（发布历史列表卡片）：保持原 图标/标签/数值 竖排 -->
        <div v-else class="stat-inner stat-inner--compact">
          <el-icon class="stat-icon" :size="13"><component :is="item.icon" /></el-icon>
          <span class="stat-label">{{ item.label }}</span>
          <span class="stat-value">{{ formatValue(item.value) }}</span>
        </div>
      </el-tooltip>
    </div>
  </div>
</template>

<script setup>
import { computed } from 'vue'
import { VideoPlay, Star, Collection, ChatLineRound } from '@element-plus/icons-vue'

const props = defineProps({
  compact: { type: Boolean, default: false },
  // 每行方块数（默认 4 列横排；数据洞察栏传 2 做 2×2 排布）
  columns: { type: Number, default: 4 },
  views: { type: [Number, String, null], default: null },
  likes: { type: [Number, String, null], default: null },
  favorites: { type: [Number, String, null], default: null },
  comments: { type: [Number, String, null], default: null },
})

// computed：props 变化时（切换账号/轮询刷新统计）四格数值实时更新
const metrics = computed(() => [
  { key: 'views', label: '播放', value: props.views, icon: VideoPlay, theme: 'blue' },
  { key: 'likes', label: '点赞', value: props.likes, icon: Star, theme: 'rose' },
  { key: 'favorites', label: '收藏', value: props.favorites, icon: Collection, theme: 'cyan' },
  { key: 'comments', label: '评论', value: props.comments, icon: ChatLineRound, theme: 'green' },
])

// 任一格有值即视为有数据：有数据时不显示占位 tooltip
const hasData = computed(() => metrics.value.some(m => m.value != null))

function formatValue(v) {
  if (v == null) return '--'
  if (typeof v === 'number') {
    if (v >= 10000) return (v / 10000).toFixed(1) + 'w'
    return v.toLocaleString('zh-CN')
  }
  return v
}

function isPlaceholder(v) {
  return v == null
}
</script>

<style lang="scss" scoped>
@use '@/styles/variables.scss' as *;

.publish-stats {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 12px;

  &--compact {
    gap: 6px;
  }
}

.stat-item {
  border: 1px solid $border;
  border-radius: $radius-base;
  background: rgba($overlay-rgb, 0.02);
  padding: 14px 16px;
  transition: $transition-base;
  cursor: default;

  &:hover {
    border-color: $border-active;
    background: rgba($overlay-rgb, 0.04);
  }

  // 紧凑模式（列表卡片内）：保持原轻量无底无框样式
  &--compact {
    padding: 8px 4px;
    border-color: transparent;
    background: transparent;

    &:hover {
      border-color: $border-light;
      background: rgba($overlay-rgb, 0.02);
    }
  }
}

// 4 个主题色（基于项目已有 platform/accent 调色板，轻量区分）
.stat-item--blue {
  background: linear-gradient(135deg, rgba($platform-channels, 0.08), rgba($overlay-rgb, 0.02));

  // 数据方块模式：主题色描边 + 主题色图标底座
  &:not(.stat-item--compact) {
    border-color: rgba($platform-channels, 0.22);

    &:hover {
      border-color: rgba($platform-channels, 0.4);
      box-shadow: 0 0 20px rgba($platform-channels, 0.08);
    }

    .stat-icon-chip {
      background: rgba($platform-channels, 0.16);

      .el-icon { color: $platform-channels; }
    }
  }

  &.stat-item--compact {
    background: linear-gradient(135deg, rgba($platform-channels, 0.04), rgba($overlay-rgb, 0.01));

    .stat-icon { color: $platform-channels; }
  }
}

.stat-item--rose {
  background: linear-gradient(135deg, rgba($accent-rose, 0.08), rgba($overlay-rgb, 0.02));

  &:not(.stat-item--compact) {
    border-color: rgba($accent-rose, 0.22);

    &:hover {
      border-color: rgba($accent-rose, 0.4);
      box-shadow: 0 0 20px rgba($accent-rose, 0.08);
    }

    .stat-icon-chip {
      background: rgba($accent-rose, 0.16);

      .el-icon { color: $accent-rose; }
    }
  }

  &.stat-item--compact {
    background: linear-gradient(135deg, rgba($accent-rose, 0.04), rgba($overlay-rgb, 0.01));

    .stat-icon { color: $accent-rose; }
  }
}

.stat-item--cyan {
  background: linear-gradient(135deg, rgba($accent-cyan, 0.08), rgba($overlay-rgb, 0.02));

  &:not(.stat-item--compact) {
    border-color: rgba($accent-cyan, 0.22);

    &:hover {
      border-color: rgba($accent-cyan, 0.4);
      box-shadow: 0 0 20px rgba($accent-cyan, 0.08);
    }

    .stat-icon-chip {
      background: rgba($accent-cyan, 0.16);

      .el-icon { color: $accent-cyan; }
    }
  }

  &.stat-item--compact {
    background: linear-gradient(135deg, rgba($accent-cyan, 0.04), rgba($overlay-rgb, 0.01));

    .stat-icon { color: $accent-cyan; }
  }
}

.stat-item--green {
  background: linear-gradient(135deg, rgba($accent-green, 0.08), rgba($overlay-rgb, 0.02));

  &:not(.stat-item--compact) {
    border-color: rgba($accent-green, 0.22);

    &:hover {
      border-color: rgba($accent-green, 0.4);
      box-shadow: 0 0 20px rgba($accent-green, 0.08);
    }

    .stat-icon-chip {
      background: rgba($accent-green, 0.16);

      .el-icon { color: $accent-green; }
    }
  }

  &.stat-item--compact {
    background: linear-gradient(135deg, rgba($accent-green, 0.04), rgba($overlay-rgb, 0.01));

    .stat-icon { color: $accent-green; }
  }
}

.stat-inner {
  display: flex;
  flex-direction: column;
  align-items: flex-start;

  &--compact {
    align-items: center;
    gap: 2px;
  }
}

// 数据方块底部：图标底座 + 标签
.stat-footer {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 10px;
}

.stat-icon-chip {
  width: 26px;
  height: 26px;
  border-radius: 8px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
}

.stat-icon {
  flex-shrink: 0;

  .stat-item--compact & {
    font-size: 13px;
  }
}

.stat-label {
  font-size: 12px;
  color: $text-muted;
  flex: 1;

  .stat-item--compact & {
    flex: none;
    font-size: 10px;
    line-height: 1.2;
  }
}

// 数值采用品牌渐变字（与仪表盘 stat-value 一致）
.stat-value {
  font-size: 22px;
  font-weight: 700;
  background: $gradient-brand;
  -webkit-background-clip: text;
  -webkit-text-fill-color: transparent;
  background-clip: text;
  line-height: 1.2;
  font-variant-numeric: tabular-nums;

  .stat-item--compact & {
    font-size: 12px;
    font-weight: 600;
    color: $text-primary;
    background: none;
    -webkit-text-fill-color: $text-primary;
  }
}

// null 占位色（统一为 muted；空值时数字/标签同时降到 muted 视觉层级，并关掉品牌渐变字）。
// 放在最后：同等优先级下覆盖主题/紧凑模式的数值配色
.stat-item--placeholder {
  .stat-label {
    color: $text-placeholder;
  }
  .stat-value {
    color: $text-placeholder;
    font-weight: 500;
    background: none;
    -webkit-text-fill-color: $text-placeholder;
  }
}
</style>
