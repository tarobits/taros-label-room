import { elementGeometry, type Element, type Preview } from './editor'

// Keep server-laid-out artwork (including overlap and clipping) while manipulating
// its geometry locally. The SVG is used as an inert image, never injected as HTML.
export function createDragPreview(preview: Preview, foreground: string, background: string) {
  const svg = new DOMParser().parseFromString(preview.svg, 'image/svg+xml')
  if (svg.querySelector('parsererror')) return null
  const namespace = 'http://www.w3.org/2000/svg'
  const layers = new Map(
    [...svg.querySelectorAll('[data-element-id]')].map((node) => {
      const wrapper = svg.createElementNS(namespace, 'g')
      node.replaceWith(wrapper)
      wrapper.append(node)
      return [node.getAttribute('data-element-id')!, wrapper] as const
    }),
  )
  if (!layers.size) return null
  const originals = new Map(preview.resolved_elements.map((element) => [element.id, element]))
  // The server SVG is monochrome; apply the tape colors to images and vectors alike.
  const channels = (hex: string) =>
    [1, 3, 5].map((offset) => parseInt(hex.slice(offset, offset + 2), 16) / 255)
  const fg = channels(foreground),
    bg = channels(background)
  const filter = svg.createElementNS(namespace, 'filter')
  filter.id = 'editor-drag-colors'
  filter.setAttribute('color-interpolation-filters', 'sRGB')
  const matrix = svg.createElementNS(namespace, 'feColorMatrix')
  matrix.setAttribute('type', 'matrix')
  matrix.setAttribute(
    'values',
    fg.map((value, index) => `${bg[index]! - value} 0 0 0 ${value}`).join(' ') + ' 0 0 0 1 0',
  )
  filter.append(matrix)
  const artwork = svg.createElementNS(namespace, 'g')
  artwork.setAttribute('filter', 'url(#editor-drag-colors)')
  artwork.append(...svg.documentElement.childNodes)
  svg.documentElement.append(filter, artwork)
  return (elements: Element[]) => {
    for (const element of elements) {
      const layer = layers.get(element.id),
        original = originals.get(element.id)
      if (!layer || !original) continue
      const before = elementGeometry(original),
        after = elementGeometry(element)
      layer.setAttribute(
        'transform',
        `translate(${after.x},${after.y}) scale(${after.width / before.width},${after.height / before.height}) translate(${-before.x},${-before.y})`,
      )
    }
    return 'data:image/svg+xml,' + encodeURIComponent(new XMLSerializer().serializeToString(svg))
  }
}
