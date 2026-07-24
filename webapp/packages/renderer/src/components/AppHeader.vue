<template>
  <div class="app-header">
    <div class="header-drag"></div>
    <div class="header-icon">
      <ArrowDownOutlined class="icon" @click="trayWin"></ArrowDownOutlined>
      <MinusOutlined class="icon" @click="minimizeWin"></MinusOutlined>
      <BorderOutlined class="icon" @click="maximizeWin"></BorderOutlined>
      <CloseOutlined class="icon" @click="closeWin"></CloseOutlined>
    </div>
  </div>
</template>

<script lang="ts">
  import {defineComponent} from 'vue';
  import {BorderOutlined, CloseOutlined, MinusOutlined, ArrowDownOutlined} from '@ant-design/icons-vue';
  import {useElectron} from '../use/electron';

  const electron = useElectron();

  export default defineComponent({
    name: 'AppHeader',
    components: {
      ArrowDownOutlined,
      MinusOutlined,
      BorderOutlined,
      CloseOutlined,
    },
    methods: {
      trayWin() {
        electron.windowControls.tray();
      },
      minimizeWin() {
        electron.windowControls.minimize();
      },
      maximizeWin() {
        electron.windowControls.maximize();
      },
      closeWin() {
        electron.windowControls.close();
      },
    },
  });
</script>

<style scoped>
  .app-header {
    position: fixed;
    left: 0;
    top: 0;
    width: 100%;
    height: 51px;
    display: flex;
    flex-direction: row;
    -webkit-app-region: drag;
    z-index: 1000;
  }

  .header-drag {
    width: 100%;
    height: 100%;
  }

  .header-icon {
    -webkit-app-region: no-drag;
    text-align: right;
    font-size: 20px;
    color: #fff;
    mix-blend-mode: difference;
    display: flex;
    align-items: center;
  }

  .icon {
    padding: 10px;
    margin-right: 5px;
  }
</style>
